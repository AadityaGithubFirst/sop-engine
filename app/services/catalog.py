"""Reference catalogs for departments, designations and tools.

Backs the searchable dropdowns in the web application. Three concerns live
here:

* **Search** - alias-aware, typo-tolerant ranking over the shipped JSON files.
* **Submissions** - entries a user typed that are not in the catalog, held in a
  pending queue rather than silently trusted.
* **Approval** - an administrator promotes a pending entry into the catalog,
  after which everyone sees it. Nothing user-typed enters the shared catalog
  without that step.

Catalog files ship read-only under `app/data/`; approved additions and the
pending queue are written to a separate writable store so a reinstall never
overwrites local additions.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Kinds of catalog entry the queue understands.
KIND_TOOL = "tool"
KIND_DEPARTMENT = "department"
KIND_ROLE = "role"
VALID_KINDS = (KIND_TOOL, KIND_DEPARTMENT, KIND_ROLE)

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Catalog file missing: %s", path)
        return {}
    except json.JSONDecodeError as exc:
        logger.error("Catalog file %s is not valid JSON: %s", path, exc)
        return {}


def _store_path() -> Path:
    """Writable store holding approved additions and the pending queue."""
    path = settings.data_path / "catalog_store.json"
    if not path.exists():
        path.write_text(
            json.dumps({"approved": [], "pending": []}, indent=2), encoding="utf-8"
        )
    return path


def _load_store() -> Dict[str, List[Dict[str, Any]]]:
    data = _read_json(_store_path())
    data.setdefault("approved", [])
    data.setdefault("pending", [])
    return data


def _save_store(store: Dict[str, Any]) -> None:
    _store_path().write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Catalog access
# ---------------------------------------------------------------------------
def _shipped(kind: str) -> List[Dict[str, Any]]:
    if kind == KIND_TOOL:
        return list(_read_json(DATA_DIR / "tools.json").get("tools", []))
    if kind == KIND_DEPARTMENT:
        return list(_read_json(DATA_DIR / "departments.json").get("departments", []))
    if kind == KIND_ROLE:
        return list(_read_json(DATA_DIR / "roles.json").get("roles", []))
    raise ValueError(f"Unknown catalog kind: {kind}")


# Parsed catalogs, keyed by kind. Invalidated when a file's mtime changes, so
# an administrator approval is picked up without a restart while keystroke
# search never re-parses 24 KB of JSON.
_cache: Dict[str, Any] = {}


def _cache_stamp(kind: str) -> tuple:
    """Modification times of every file feeding a catalog."""
    paths = [DATA_DIR / f"{kind}s.json", settings.data_path / "catalog_store.json"]
    stamps = []
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def entries(kind: str) -> List[Dict[str, Any]]:
    """Shipped catalog plus admin-approved additions, de-duplicated by name."""
    if kind not in VALID_KINDS:
        raise ValueError(f"Unknown catalog kind: {kind}")

    stamp = _cache_stamp(kind)
    cached = _cache.get(kind)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    combined = _shipped(kind)
    seen = {str(item.get("name", "")).casefold() for item in combined}
    for item in _load_store()["approved"]:
        if item.get("kind") != kind:
            continue
        name = str(item.get("name", ""))
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        combined.append(
            {
                "name": name,
                "category": item.get("category", "Locally Approved"),
                "aliases": item.get("aliases", []),
                "description": item.get("description", ""),
                "locally_approved": True,
            }
        )

    _cache[kind] = (stamp, combined)
    return combined


def categories(kind: str) -> List[str]:
    """Distinct category labels present in a catalog, for grouped dropdowns."""
    labels = {str(item.get("category") or item.get("tier") or "Other") for item in entries(kind)}
    return sorted(labels)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _score(query: str, entry: Dict[str, Any]) -> float:
    """Rank an entry against a query.

    Ordering intent: exact name > name prefix > alias hit > substring >
    fuzzy similarity. Returns 0.0 when the entry should not be shown.
    """
    query = query.casefold().strip()
    if not query:
        return 0.1  # everything is equally (un)interesting with no query

    name = str(entry.get("name", "")).casefold()
    aliases = [str(a).casefold() for a in entry.get("aliases", [])]

    if name == query or query in aliases:
        return 1.0
    if name.startswith(query):
        return 0.9
    if any(alias.startswith(query) for alias in aliases):
        return 0.85
    if query in name:
        return 0.75
    if any(query in alias for alias in aliases):
        return 0.7
    if query in str(entry.get("description", "")).casefold():
        return 0.5

    # Typo tolerance: "postgrez" should still find PostgreSQL. This is the only
    # expensive branch, so it is gated: very short queries are too ambiguous to
    # fuzzy-match usefully, and strings of wildly different length can never
    # clear the threshold, so neither is worth the comparison.
    if len(query) < 4:
        return 0.0

    def close_enough(candidate: str) -> float:
        if not candidate:
            return 0.0
        shorter, longer = sorted((len(query), len(candidate)))
        if shorter / longer < 0.6:  # length ratio caps the achievable score
            return 0.0
        return SequenceMatcher(None, query, candidate).ratio()

    ratio = close_enough(name)
    if ratio < 0.62:
        for alias in aliases:
            ratio = max(ratio, close_enough(alias))
            if ratio >= 0.62:
                break
    return ratio if ratio >= 0.62 else 0.0


def search(kind: str, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
    """Ranked catalog search used by the type-ahead dropdowns."""
    scored = []
    for entry in entries(kind):
        score = _score(query, entry)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name", ""))))
    return [entry for _score_value, entry in scored[:limit]]


def find(kind: str, name: str) -> Optional[Dict[str, Any]]:
    """Exact (case-insensitive) lookup by name or alias."""
    target = name.casefold().strip()
    for entry in entries(kind):
        if str(entry.get("name", "")).casefold() == target:
            return entry
        if any(str(alias).casefold() == target for alias in entry.get("aliases", [])):
            return entry
    return None


def is_known(kind: str, name: str) -> bool:
    """True when the name already exists in the catalog."""
    return find(kind, name) is not None


# ---------------------------------------------------------------------------
# Submission queue
# ---------------------------------------------------------------------------
def submit(
    kind: str,
    name: str,
    description: str = "",
    category: str = "",
    submitted_by: str = "unattributed",
    source: str = "user",
) -> Dict[str, Any]:
    """Queue a user-supplied entry for administrator review.

    Submitting is idempotent per name+kind: re-submitting an already-queued
    entry returns the existing record rather than creating a duplicate.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"Unknown catalog kind: {kind}")
    name = name.strip()
    if not name:
        raise ValueError("Entry name must not be blank.")

    with _lock:
        store = _load_store()
        for existing in store["pending"]:
            if existing["kind"] == kind and existing["name"].casefold() == name.casefold():
                return existing

        record = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "name": name,
            "category": category or "Pending Review",
            "description": description.strip(),
            "aliases": [],
            "submitted_by": submitted_by,
            "source": source,  # user | web_lookup | model
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        store["pending"].append(record)
        _save_store(store)

    logger.info("Queued %s '%s' for admin approval (id=%s)", kind, name, record["id"])
    return record


def list_pending(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Everything awaiting administrator review."""
    pending = _load_store()["pending"]
    return [item for item in pending if kind is None or item["kind"] == kind]


def list_approved(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Everything an administrator has promoted into the catalog."""
    approved = _load_store()["approved"]
    return [item for item in approved if kind is None or item["kind"] == kind]


def approve(entry_id: str, approved_by: str = "administrator") -> Dict[str, Any]:
    """Promote a pending entry into the shared catalog."""
    with _lock:
        store = _load_store()
        for index, item in enumerate(store["pending"]):
            if item["id"] == entry_id:
                record = store["pending"].pop(index)
                record["status"] = "approved"
                record["approved_by"] = approved_by
                record["approved_at"] = datetime.now(timezone.utc).isoformat()
                store["approved"].append(record)
                _save_store(store)
                logger.info("Approved %s '%s'", record["kind"], record["name"])
                return record
    raise KeyError(f"No pending catalog entry with id '{entry_id}'.")


def reject(entry_id: str, reason: str = "", rejected_by: str = "administrator") -> Dict[str, Any]:
    """Remove a pending entry without adding it to the catalog."""
    with _lock:
        store = _load_store()
        for index, item in enumerate(store["pending"]):
            if item["id"] == entry_id:
                record = store["pending"].pop(index)
                record.update(
                    status="rejected",
                    rejected_by=rejected_by,
                    reason=reason,
                    rejected_at=datetime.now(timezone.utc).isoformat(),
                )
                _save_store(store)
                logger.info("Rejected %s '%s': %s", record["kind"], record["name"], reason)
                return record
    raise KeyError(f"No pending catalog entry with id '{entry_id}'.")


def stats() -> Dict[str, Any]:
    """Counts for the administrator screen."""
    store = _load_store()
    return {
        "tools": len(entries(KIND_TOOL)),
        "departments": len(entries(KIND_DEPARTMENT)),
        "roles": len(entries(KIND_ROLE)),
        "pending": len(store["pending"]),
        "approved_additions": len(store["approved"]),
    }


def unknown_names(kind: str, names: Iterable[str]) -> List[str]:
    """Subset of `names` that the catalog does not recognise."""
    return [name for name in names if name.strip() and not is_known(kind, name)]
