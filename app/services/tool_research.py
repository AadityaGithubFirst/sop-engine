"""Unknown-tool research with a human in the loop.

When a user names a tool the catalog does not hold, the engine drafts a
description and shows it for approval. The user accepts it, rejects it (with an
optional hint, producing a refined draft), or writes their own text — and
whatever is finally accepted goes into the SOP and into the pending queue for
an administrator to promote into the shared catalog.

Three sources, tried in order:

1. **Catalog** - already known; no drafting needed.
2. **Local model** - the offline model describes the tool. This is the default
   because it preserves air-gapped operation.
3. **Public reference sites** - only when `ALLOW_WEB_LOOKUP` is explicitly
   enabled. This sends the tool name off the machine, so it is off by default
   and every call is logged.

The user's own text is always available and always wins.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services import catalog, preferences
from app.services.ollama_client import (
    OllamaClient,
    OllamaTimeoutError,
    OllamaUnavailableError,
    get_client,
)

logger = logging.getLogger(__name__)

SOURCE_CATALOG = "catalog"
SOURCE_MODEL = "local_model"
SOURCE_WEB = "web_lookup"
SOURCE_USER = "user"

_WEB_TIMEOUT = 10
_USER_AGENT = "SOP-Engine/1.0 (offline government documentation tool)"

SYSTEM_INSTRUCTION = """\
You are a government IT systems cataloguer. You are given the name of a software
tool, platform, or system and must describe it factually for inclusion in an
official Standard Operating Procedure.

Reply with STRICT JSON only - no prose, no markdown fence:
{
  "known": true or false,
  "name": "the tool's correct full name",
  "category": "one of: Database, Language / Runtime, Business Intelligence, Infrastructure, Data Pipeline, GIS, Security / Identity, Monitoring, Collaboration, Engineering, Analytics, Government Platform (India), Government Platform (Maharashtra), Other",
  "description": "two or three factual sentences: what it is, what it is used for, and who typically operates it",
  "typical_version": "the current or most common version, or an empty string",
  "confidence": "high, medium, or low"
}

Rules:
- If you do not recognise the tool, set "known": false and "confidence": "low",
  and leave "description" empty. NEVER invent a description for a tool you do
  not recognise - a wrong description in a government SOP is worse than none.
- Never include installation commands here; that comes later.
"""

_REFINE_SUFFIX = """

The reviewing officer REJECTED your previous description(s):
{rejected}

Their guidance: {hint}

Produce a corrected description that addresses the guidance. If you still do
not recognise this tool, set "known": false rather than guessing again.
"""


@dataclass
class ToolDraft:
    """A proposed description awaiting the user's decision."""

    name: str
    description: str = ""
    category: str = "Other"
    typical_version: str = ""
    source: str = SOURCE_MODEL
    confidence: str = "low"
    known: bool = False
    attempt: int = 1
    in_catalog: bool = False
    needs_user_input: bool = False
    message: str = ""
    references: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Source 1: catalog
# ---------------------------------------------------------------------------
def from_catalog(name: str) -> Optional[ToolDraft]:
    """Return a draft built from the shipped catalog, if the tool is known."""
    entry = catalog.find(catalog.KIND_TOOL, name)
    if entry is None:
        return None
    return ToolDraft(
        name=str(entry.get("name", name)),
        description=str(entry.get("description", "")),
        category=str(entry.get("category", "Other")),
        source=SOURCE_CATALOG,
        confidence="high",
        known=True,
        in_catalog=True,
        message="This tool is already in the catalog.",
    )


# ---------------------------------------------------------------------------
# Source 2: local model (air-gapped)
# ---------------------------------------------------------------------------
def _parse_model_json(raw: str) -> Dict[str, Any]:
    """Extract the JSON object from a model reply, tolerating fences and prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _research_model(client: OllamaClient) -> str:
    """Pick the quickest installed model for a short lookup.

    Reasoning models emit a long internal monologue before answering, which is
    wasted effort here. If a lighter model is installed, use it; otherwise fall
    back to whatever the client is configured with.
    """
    active = client.model
    if not _is_reasoning_model(active):
        return active

    installed = client.list_models()
    for candidate in (settings.FAST_MODEL, "llama3.2:3b", "llama3.2:1b", "qwen2.5:7b"):
        for name in installed:
            if name == candidate or name.split(":")[0] == candidate.split(":")[0]:
                logger.info("Using %s for tool research instead of %s", name, active)
                return name
    return active


def _is_reasoning_model(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in ("deepseek-r1", "qwq", "-r1", "reason"))


def from_local_model(
    name: str,
    client: Optional[OllamaClient] = None,
    rejected: Optional[List[str]] = None,
    hint: str = "",
    attempt: int = 1,
) -> ToolDraft:
    """Ask the offline model to describe the tool.

    Rejected drafts and the user's hint are fed back so each attempt is a
    genuine refinement rather than a re-roll of the same answer.
    """
    client = client or get_client()
    research_model = _research_model(client)
    prompt = f"Tool name: {name}\n\nDescribe this tool."
    if rejected:
        prompt += _REFINE_SUFFIX.format(
            rejected="\n".join(f"- {text}" for text in rejected if text),
            hint=hint or "The previous description was not accurate enough.",
        )

    try:
        raw = client.generate(
            system_prompt=SYSTEM_INSTRUCTION,
            user_prompt=prompt,
            # Describing a tool is a lookup, not an essay. Prefer the fast model,
            # cap the reply, skip chain-of-thought, and give up quickly - this is
            # an interactive control, and a reasoning model on CPU would take
            # minutes to answer a one-line question.
            model=research_model,
            # A reasoning model spends most of its budget on the internal
            # monologue before it writes anything, so capping it at the answer
            # length leaves nothing for the answer itself.
            num_predict=(
                settings.RESEARCH_NUM_PREDICT * 4
                if _is_reasoning_model(research_model)
                else settings.RESEARCH_NUM_PREDICT
            ),
            num_ctx=1024,
            timeout=settings.RESEARCH_TIMEOUT,
            think=False,
        )
    except OllamaTimeoutError as exc:
        logger.warning("Tool research timed out: %s", exc)
        return ToolDraft(
            name=name,
            source=SOURCE_MODEL,
            attempt=attempt,
            needs_user_input=True,
            message=(
                f"The model took too long to answer (over {settings.RESEARCH_TIMEOUT} seconds "
                f"on this computer). Please describe this tool yourself — it is quicker. "
                f"{exc.hint}"
            ),
        )
    except OllamaUnavailableError as exc:
        logger.warning("Local model unavailable for tool research: %s", exc)
        return ToolDraft(
            name=name,
            source=SOURCE_MODEL,
            attempt=attempt,
            needs_user_input=True,
            message=(
                "The local model is not running, so no description could be drafted. "
                "Please describe this tool yourself."
            ),
        )

    data = _parse_model_json(raw)
    known = bool(data.get("known")) and bool(str(data.get("description", "")).strip())

    if not known:
        return ToolDraft(
            name=str(data.get("name") or name),
            source=SOURCE_MODEL,
            attempt=attempt,
            confidence="low",
            known=False,
            needs_user_input=True,
            message=(
                f"The offline model does not recognise '{name}'. "
                "Please describe it yourself, or enable web lookup in settings."
            ),
        )

    return ToolDraft(
        name=str(data.get("name") or name),
        description=str(data.get("description", "")).strip(),
        category=str(data.get("category") or "Other"),
        typical_version=str(data.get("typical_version") or ""),
        source=SOURCE_MODEL,
        confidence=str(data.get("confidence") or "medium"),
        known=True,
        attempt=attempt,
        message="Drafted by the offline model. Please check it before accepting.",
    )


# ---------------------------------------------------------------------------
# Source 3: public reference sites (opt-in, breaks air-gap)
# ---------------------------------------------------------------------------
def _http_get_json(url: str) -> Optional[Dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_WEB_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.info("Web lookup failed for %s: %s", url, exc)
        return None


def from_web(name: str, attempt: int = 1) -> ToolDraft:
    """Look the tool up on public reference sites.

    Only ever called when `ALLOW_WEB_LOOKUP` is on. Sends the tool name and
    nothing else; project data never leaves the machine.
    """
    if not preferences.web_lookup_enabled():
        return ToolDraft(
            name=name,
            source=SOURCE_WEB,
            attempt=attempt,
            needs_user_input=True,
            message=(
                "Internet lookup is switched off, which keeps this installation fully "
                "offline. You can turn it on under Settings."
            ),
        )

    logger.warning(
        "WEB LOOKUP: sending tool name '%s' to public reference sites "
        "(ALLOW_WEB_LOOKUP is enabled)",
        name,
    )

    quoted = urllib.parse.quote(name.replace(" ", "_"))
    references: List[str] = []

    summary = _http_get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quoted}")
    if summary and summary.get("extract") and summary.get("type") != "disambiguation":
        references.append(
            str(summary.get("content_urls", {}).get("desktop", {}).get("page", ""))
        )
        return ToolDraft(
            name=str(summary.get("title") or name),
            description=str(summary["extract"]).strip(),
            category="Other",
            source=SOURCE_WEB,
            confidence="medium",
            known=True,
            attempt=attempt,
            references=[ref for ref in references if ref],
            message="Drafted from a public reference site. Please verify before accepting.",
        )

    # DuckDuckGo's instant-answer endpoint covers many products Wikipedia misses.
    ddg = _http_get_json(
        "https://api.duckduckgo.com/?"
        + urllib.parse.urlencode({"q": name, "format": "json", "no_html": "1"})
    )
    if ddg and str(ddg.get("AbstractText", "")).strip():
        return ToolDraft(
            name=str(ddg.get("Heading") or name),
            description=str(ddg["AbstractText"]).strip(),
            source=SOURCE_WEB,
            confidence="low",
            known=True,
            attempt=attempt,
            references=[str(ddg.get("AbstractURL", ""))],
            message="Drafted from a public reference site. Please verify before accepting.",
        )

    return ToolDraft(
        name=name,
        source=SOURCE_WEB,
        attempt=attempt,
        needs_user_input=True,
        message=(
            f"No public reference was found for '{name}'. Please describe it yourself."
        ),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def research(
    name: str,
    attempt: int = 1,
    rejected: Optional[List[str]] = None,
    hint: str = "",
    client: Optional[OllamaClient] = None,
    allow_web: Optional[bool] = None,
) -> ToolDraft:
    """Produce the next draft description for `name`.

    Called once per user iteration: attempt 1 is the first proposal, and each
    rejection increments `attempt` with the rejected text fed back in.
    """
    name = name.strip()
    if not name:
        raise ValueError("Tool name must not be blank.")

    if attempt <= 1 and not rejected:
        known = from_catalog(name)
        if known is not None:
            return known

    draft = from_local_model(
        name, client=client, rejected=rejected, hint=hint, attempt=attempt
    )

    # Fall through to the web only if the model could not help and the operator
    # has accepted the trade-off.
    web_permitted = preferences.web_lookup_enabled() if allow_web is None else allow_web
    if draft.needs_user_input and web_permitted:
        web_draft = from_web(name, attempt=attempt)
        if not web_draft.needs_user_input:
            return web_draft

    return draft


def accept(
    name: str,
    description: str,
    category: str = "",
    source: str = SOURCE_USER,
    submitted_by: str = "unattributed",
) -> Dict[str, Any]:
    """Record an accepted description and queue it for administrator approval.

    The description is usable in the SOP immediately; the queue only governs
    whether it becomes visible to *other* users.
    """
    name = name.strip()
    description = description.strip()
    if not name:
        raise ValueError("Tool name must not be blank.")

    if catalog.is_known(catalog.KIND_TOOL, name):
        entry = catalog.find(catalog.KIND_TOOL, name) or {}
        return {
            "queued": False,
            "reason": "already_in_catalog",
            "tool": {
                "name": entry.get("name", name),
                "description": entry.get("description", description),
                "category": entry.get("category", category or "Other"),
            },
        }

    record = catalog.submit(
        kind=catalog.KIND_TOOL,
        name=name,
        description=description,
        category=category or "Pending Review",
        submitted_by=submitted_by,
        source=source,
    )
    return {
        "queued": True,
        "reason": "awaiting_admin_approval",
        "submission_id": record["id"],
        "tool": {"name": name, "description": description, "category": record["category"]},
    }
