"""Operator preferences that can be changed at runtime.

Distinct from `app.config.Settings`, which is deployment configuration read
once at startup. These are choices an operator makes *while using* the tool and
expects to persist — chiefly whether the engine may leave the machine.

Stored next to the catalog, outside the install directory, so an upgrade never
silently re-enables something the office turned off. Every change to a
privacy-affecting preference is logged with its old and new value so the
decision is auditable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Preferences that change whether data can leave the machine. Changes to these
# are logged at WARNING so they surface in any log review.
_PRIVACY_KEYS = {"web_lookup"}

_DEFAULTS: Dict[str, Any] = {
    # Falls back to the deployment default until an operator overrides it.
    "web_lookup": None,
}


def _path():
    return settings.data_path / "preferences.json"


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        _path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk-full or read-only volume
        logger.error("Could not persist preferences: %s", exc)


def get(key: str, default: Any = None) -> Any:
    """Read a preference, falling back to its stored default."""
    stored = _load().get(key, _DEFAULTS.get(key))
    return default if stored is None and default is not None else stored


def set_value(key: str, value: Any, changed_by: str = "operator") -> Dict[str, Any]:
    """Write a preference and return the full preference record."""
    with _lock:
        data = _load()
        previous = data.get(key)
        data[key] = value
        data.setdefault("_history", [])
        data["_history"] = (data["_history"] + [{
            "key": key,
            "from": previous,
            "to": value,
            "changed_by": changed_by,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }])[-50:]
        _save(data)

    if key in _PRIVACY_KEYS:
        logger.warning(
            "PRIVACY SETTING CHANGED: %s %s -> %s (by %s)", key, previous, value, changed_by
        )
    else:
        logger.info("Preference %s set to %s", key, value)
    return {"key": key, "value": value, "previous": previous}


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------
def web_lookup_enabled() -> bool:
    """Whether the engine may consult public reference sites.

    The operator's runtime choice wins; absent one, the deployment default in
    `.env` applies. Defaulting to the deployment value (rather than True) means
    an air-gapped install stays air-gapped even if this file is missing.
    """
    override = _load().get("web_lookup")
    if override is None:
        return bool(settings.ALLOW_WEB_LOOKUP)
    return bool(override)


def snapshot() -> Dict[str, Any]:
    """Current effective preferences, for the settings screen."""
    stored = _load()
    return {
        "web_lookup": web_lookup_enabled(),
        "web_lookup_source": "operator" if stored.get("web_lookup") is not None else "config",
        "web_lookup_config_default": bool(settings.ALLOW_WEB_LOOKUP),
    }
