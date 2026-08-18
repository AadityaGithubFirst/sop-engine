"""Thin, defensive wrapper around the local Ollama daemon.

All inference happens on the loopback interface. Nothing in this module
performs an outbound network call to a third-party provider.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard exercised only without the dependency
    import ollama
    from ollama import Client as OllamaSDKClient

    _OLLAMA_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover
    ollama = None  # type: ignore[assignment]
    OllamaSDKClient = None  # type: ignore[assignment]
    _OLLAMA_IMPORT_ERROR = exc


class OllamaUnavailableError(RuntimeError):
    """Raised when the local Ollama daemon cannot serve a request.

    Carries a remediation hint so the API layer can return something the
    operator can act on without reading server logs.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint or (
            "Start the daemon with `ollama serve`, then pull a model with "
            f"`ollama pull {settings.MODEL_NAME}`."
        )


class OllamaTimeoutError(OllamaUnavailableError):
    """The daemon is alive but did not answer inside the deadline.

    Subclasses `OllamaUnavailableError` so existing handlers keep working;
    callers that care about the difference can catch this first.
    """


def auto_num_ctx(model: str, configured: int) -> int:
    """Pick a context window sized to the model's parameter count.

    Bigger models both reason and write more, so a window tuned for an 8B model
    truncates a 32B model's section mid-table. The returned value is never below
    the operator's configured `NUM_CTX` and never above `NUM_CTX_CAP`, so RAM use
    stays bounded and an explicit `NUM_CTX` is always respected as a floor.
    A model whose size cannot be read from its tag keeps the configured value.
    """
    import re

    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model.lower())
    if not match:
        return configured

    billions = float(match.group(1))
    if billions <= 8:
        target = 8192
    elif billions <= 15:
        target = 16384
    elif billions <= 34:
        target = 24576
    else:
        target = 32768

    return max(configured, min(target, settings.NUM_CTX_CAP))


def _looks_like_timeout(exc: Exception) -> bool:
    """True when an SDK exception represents a deadline overrun."""
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "timeout" in str(exc).lower()


class OllamaClient:
    """Chat/completion facade with health checks and readable failures."""

    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.host = host or settings.OLLAMA_HOST
        self.model = model or settings.MODEL_NAME
        self.timeout = timeout or settings.REQUEST_TIMEOUT
        self._client: Any = None

    # -- connection ---------------------------------------------------------
    @property
    def client(self) -> Any:
        """Lazily construct the SDK client so import never blocks startup."""
        if self._client is None:
            if OllamaSDKClient is None:
                raise OllamaUnavailableError(
                    f"The `ollama` package is not importable: {_OLLAMA_IMPORT_ERROR}",
                    hint="Install dependencies with `pip install -r requirements.txt`.",
                )
            self._client = OllamaSDKClient(host=self.host, timeout=self.timeout)
        return self._client

    # -- health -------------------------------------------------------------
    def list_models(self) -> List[str]:
        """Return the model tags installed locally. Empty list if unreachable."""
        try:
            response = self.client.list()
        except Exception as exc:
            logger.warning("Could not list Ollama models at %s: %s", self.host, exc)
            return []

        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])
        names: List[str] = []
        for entry in models or []:
            name = getattr(entry, "model", None) or getattr(entry, "name", None)
            if name is None and isinstance(entry, dict):
                name = entry.get("model") or entry.get("name")
            if name:
                names.append(str(name))
        return names

    def health(self) -> Dict[str, Any]:
        """Report daemon reachability and whether the configured model is present."""
        available = self.list_models()
        reachable = bool(available)
        detail: Optional[str] = None
        if not reachable:
            detail = (
                f"No response from the Ollama daemon at {self.host}. "
                "It may be stopped, or no models have been pulled yet."
            )

        # Ollama tags are `name:tag`; treat a bare name as matching any tag.
        wanted = self.model
        model_available = any(
            tag == wanted or tag.split(":")[0] == wanted.split(":")[0] for tag in available
        )
        if reachable and not model_available:
            detail = f"Model '{wanted}' is not installed. Run `ollama pull {wanted}`."

        return {
            "ollama_reachable": reachable,
            "model_available": model_available,
            "available_models": available,
            "detail": detail,
        }

    # -- inference ----------------------------------------------------------
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        num_ctx: Optional[int] = None,
        num_predict: Optional[int] = None,
        timeout: Optional[int] = None,
        think: Optional[bool] = None,
    ) -> str:
        """Run a single system+user chat turn and return the assistant text.

        `num_predict` caps the reply length and `timeout` overrides the default
        deadline: short interactive lookups must not inherit the ten-minute
        budget that whole-document generation needs. `think=False` asks a
        reasoning model to skip its chain of thought, which is the single
        biggest saving on a CPU-only machine.

        When `think` is not given, it defaults to `settings.ENABLE_THINKING`
        (off), so document passes ask the model to answer directly. When
        `num_ctx` is not given and `settings.AUTO_NUM_CTX` is on, the window is
        scaled to the model's size via `auto_num_ctx`. An explicit argument
        always wins over both defaults.
        """
        target_model = model or self.model

        effective_think = settings.ENABLE_THINKING if think is None else think

        if num_ctx is not None:
            effective_num_ctx = num_ctx
        elif settings.AUTO_NUM_CTX:
            effective_num_ctx = auto_num_ctx(target_model, settings.NUM_CTX)
        else:
            effective_num_ctx = settings.NUM_CTX

        options = {
            "temperature": settings.TEMPERATURE if temperature is None else temperature,
            "num_ctx": effective_num_ctx,
        }
        if num_predict is not None:
            options["num_predict"] = num_predict

        # A short-deadline call needs its own client; the SDK fixes the timeout
        # at construction time.
        client = self.client
        if timeout is not None and timeout != self.timeout:
            if OllamaSDKClient is None:  # pragma: no cover - guarded in `client`
                raise OllamaUnavailableError("The `ollama` package is not importable.")
            client = OllamaSDKClient(host=self.host, timeout=timeout)

        request: Dict[str, Any] = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": options,
        }

        try:
            try:
                # `think` reached the SDK in 0.4; older builds reject it.
                response = client.chat(**request, think=effective_think)
            except TypeError:
                response = client.chat(**request)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            # A deadline overrun and a dead daemon need different advice, and
            # telling an operator "Ollama is not running" when it is merely slow
            # sends them to fix the wrong thing.
            if _looks_like_timeout(exc):
                raise OllamaTimeoutError(
                    f"'{target_model}' did not answer within "
                    f"{timeout or self.timeout} seconds.",
                    hint=(
                        "This model is slow on this computer. Install a lighter one "
                        "with `ollama pull llama3.2:3b` for quick lookups."
                    ),
                ) from exc
            raise OllamaUnavailableError(
                f"Local inference failed against {self.host} using '{target_model}': {exc}"
            ) from exc

        return strip_reasoning(_extract_message_content(response))


def _extract_message_content(response: Any) -> str:
    """Pull assistant text out of either an SDK object or a plain dict."""
    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message")

    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")

    if content is None and isinstance(response, dict):
        content = response.get("response")

    return (content or "").strip()


def strip_reasoning(text: str) -> str:
    """Remove `<think>...</think>` blocks emitted by reasoning models.

    deepseek-r1 streams its chain of thought inline; it must never reach a
    government document.
    """
    if "<think>" not in text:
        return text.strip()

    cleaned: List[str] = []
    cursor = 0
    while True:
        start = text.find("<think>", cursor)
        if start == -1:
            cleaned.append(text[cursor:])
            break
        cleaned.append(text[cursor:start])
        end = text.find("</think>", start)
        if end == -1:
            # Unterminated block: the reply ran out of budget mid-thought, so
            # no closing tag (and usually no answer) was ever emitted. We keep
            # the text before `<think>` and drop the truncated remainder, but we
            # warn loudly — a silent empty return here is what makes the caller
            # fall back to boilerplate with no visible cause. Disabling thinking
            # (`think=False`) or raising NUM_CTX is the real fix.
            logger.warning(
                "Reasoning block was never closed (%d chars of unterminated "
                "<think> dropped); the model likely exhausted its context "
                "before answering. Consider think=False or a larger NUM_CTX.",
                len(text) - start,
            )
            break
        cursor = end + len("</think>")
    return "".join(cleaned).strip()


_default_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    """Process-wide client singleton."""
    global _default_client
    if _default_client is None:
        _default_client = OllamaClient()
    return _default_client
