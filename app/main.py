"""FastAPI entrypoint for the Autonomous SOP Generation Engine.

Serves both the JSON API and the built-in web application. All inference runs
on a local Ollama daemon; no request made by this service leaves the host.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fastapi import Header

from app.agents.orchestrator import MasterOrchestrator
from app.config import settings
from app.schemas import (
    AdminDecisionRequest,
    CatalogSubmitRequest,
    HealthResponse,
    ProjectPayload,
    SOPResponse,
    ToolAcceptRequest,
    ToolResearchRequest,
    WebLookupRequest,
)
from app.services import catalog, hardware, preferences, tool_research
from app.services.document_exporter import (
    DocumentExportError,
    export_markdown_to_docx,
    resolve_docx_path,
)
from app.services.ollama_client import OllamaClient, OllamaUnavailableError, get_client
from app.services.validators import SOPValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("sop_engine")

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Offline, zero-cost, multi-agent Standard Operating Procedure generator "
        "for government projects. Three validated generation passes run on a "
        "local Ollama model - no cloud API keys, no external data transfer."
    ),
)


@app.exception_handler(OllamaUnavailableError)
async def _ollama_unavailable_handler(_request, exc: OllamaUnavailableError):
    """Turn a dead local daemon into an actionable 503 rather than a 500."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc), "hint": exc.hint},
    )


@app.exception_handler(SOPValidationError)
async def _validation_failed_handler(_request, exc: SOPValidationError):
    """Report which structural gate rejected the document, and why."""
    return JSONResponse(
        status_code=422,  # Unprocessable Content
        content={
            "detail": str(exc),
            "hint": (
                "The local model could not produce a document meeting the structural "
                "standard. Try a larger model (for example llama3.1:8b) or add more "
                "detail to the project description."
            ),
            "validation": exc.report.model_dump(),
        },
    )


@app.get("/api/v1/info", tags=["meta"])
def info() -> dict:
    """Service banner and route index."""
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "runtime": "local-ollama",
        "model": settings.MODEL_NAME,
        "offline": True,
        "pipeline": [
            "pass-1-governance-entity-integrity",
            "pass-2-technical-stack-matching",
            "pass-3-deep-operational-execution",
            "post-generation-validation-gate",
        ],
        "endpoints": {
            "app": "GET /",
            "generate": "POST /api/v1/sop/generate",
            "download": "GET /api/v1/sop/download/{document_id}",
            "health": "GET /api/v1/health",
            "docs": "GET /docs",
        },
    }


@app.get("/api/v1/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Report whether the local model runtime is ready to serve requests."""
    client = get_client()
    report = client.health()
    ready = report["ollama_reachable"] and report["model_available"]
    return HealthResponse(
        status="ready" if ready else "degraded",
        app_version=settings.APP_VERSION,
        ollama_host=settings.OLLAMA_HOST,
        model_name=settings.MODEL_NAME,
        **report,
    )


@app.post(
    "/api/v1/sop/generate",
    response_model=SOPResponse,
    status_code=status.HTTP_200_OK,
    tags=["sop"],
)
def generate_sop(payload: ProjectPayload) -> SOPResponse:
    """Run the three-pass pipeline and export the validated `.docx`."""
    # A per-request model override lets an officer trade speed for depth
    # without an administrator editing configuration files.
    if payload.model_override:
        orchestrator = MasterOrchestrator(client=OllamaClient(model=payload.model_override))
    else:
        orchestrator = MasterOrchestrator()

    document_id, markdown, metadata, report = orchestrator.generate(payload)

    try:
        export_markdown_to_docx(markdown=markdown, document_id=document_id, title=None)
    except DocumentExportError as exc:
        logger.exception("Export failed for %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    logger.info(
        "Generated %s (%d chars, %d repair attempts) using %s",
        document_id,
        len(markdown),
        metadata.repair_attempts,
        metadata.model_name,
    )
    return SOPResponse(
        document_id=document_id,
        markdown_content=markdown,
        docx_download_url=(
            f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/sop/download/{document_id}"
        ),
        validation=report,
    )


@app.get("/api/v1/sop/download/{document_id}", tags=["sop"])
def download_sop(document_id: str) -> FileResponse:
    """Serve a previously generated `.docx` by its document identifier."""
    path = resolve_docx_path(document_id)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No generated document found for id '{document_id}'.",
        )
    return FileResponse(path=str(path), media_type=DOCX_MEDIA_TYPE, filename=path.name)



# ---------------------------------------------------------------------------
# Machine profiling and model selection
# ---------------------------------------------------------------------------
@app.get("/api/v1/system/profile", tags=["system"])
def system_profile() -> dict:
    """Detect this computer's capability and recommend a model tier."""
    report = hardware.recommend()
    report["active_model"] = settings.MODEL_NAME
    report["installed_models"] = get_client().list_models()
    return report


# ---------------------------------------------------------------------------
# Reference catalogs (searchable dropdowns)
# ---------------------------------------------------------------------------
@app.get("/api/v1/catalog/{kind}", tags=["catalog"])
def catalog_search(kind: str, q: str = "", limit: int = 25) -> dict:
    """Type-ahead search over tools, departments, or designations."""
    try:
        results = catalog.search(kind, q, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "kind": kind,
        "query": q,
        "count": len(results),
        "results": results,
        # Reuse the ranked results instead of a second full catalog scan.
        "exact_match": any(
            str(entry.get("name", "")).casefold() == q.casefold() for entry in results
        ) if q else False,
    }


@app.post("/api/v1/catalog/submit", tags=["catalog"])
def catalog_submit(request: CatalogSubmitRequest) -> dict:
    """Queue a user-proposed department, designation, or tool for approval."""
    try:
        record = catalog.submit(
            kind=request.kind,
            name=request.name,
            description=request.description,
            category=request.category,
            submitted_by=request.submitted_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"queued": True, "entry": record}


# ---------------------------------------------------------------------------
# Unknown-tool research loop
# ---------------------------------------------------------------------------
@app.post("/api/v1/tools/research", tags=["catalog"])
def research_tool(request: ToolResearchRequest) -> dict:
    """Draft a description for an unlisted tool, refining on each rejection."""
    try:
        draft = tool_research.research(
            name=request.name,
            attempt=request.attempt,
            rejected=request.rejected,
            hint=request.hint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft.as_dict()


@app.post("/api/v1/tools/accept", tags=["catalog"])
def accept_tool(request: ToolAcceptRequest) -> dict:
    """Record an accepted description and queue it for administrator approval."""
    try:
        return tool_research.accept(
            name=request.name,
            description=request.description,
            category=request.category,
            source=request.source,
            submitted_by=request.submitted_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Administrator screen
# ---------------------------------------------------------------------------
def _require_admin(token: str) -> None:
    """Gate the approval endpoints behind the shared administrator token."""
    if token != settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid administrator token is required for this action.",
        )


@app.get("/api/v1/admin/pending", tags=["admin"])
def admin_pending(x_admin_token: str = Header("")) -> dict:
    """Everything awaiting administrator review."""
    _require_admin(x_admin_token)
    return {"stats": catalog.stats(), "pending": catalog.list_pending()}


@app.post("/api/v1/admin/approve", tags=["admin"])
def admin_approve(request: AdminDecisionRequest, x_admin_token: str = Header("")) -> dict:
    """Promote a queued entry into the shared catalog."""
    _require_admin(x_admin_token)
    try:
        return {"approved": catalog.approve(request.entry_id, request.decided_by)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/admin/reject", tags=["admin"])
def admin_reject(request: AdminDecisionRequest, x_admin_token: str = Header("")) -> dict:
    """Discard a queued entry without adding it to the catalog."""
    _require_admin(x_admin_token)
    try:
        return {"rejected": catalog.reject(request.entry_id, request.reason, request.decided_by)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



# ---------------------------------------------------------------------------
# Operator preferences
# ---------------------------------------------------------------------------
@app.get("/api/v1/settings", tags=["system"])
def read_settings() -> dict:
    """Preferences an operator can change from the settings screen."""
    return preferences.snapshot()


@app.post("/api/v1/settings/web-lookup", tags=["system"])
def set_web_lookup(request: WebLookupRequest) -> dict:
    """Turn internet lookup for unknown tools on or off.

    This is the only control that changes whether the engine may leave the
    machine, so the change is persisted and written to the log.
    """
    preferences.set_value("web_lookup", bool(request.enabled), changed_by=request.changed_by)
    return preferences.snapshot()


# The web application is mounted last so it never shadows an API route.
# `html=True` serves index.html at "/".
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="app")
else:  # pragma: no cover - only when the static bundle is missing
    logger.warning("Web application assets not found at %s; API-only mode.", STATIC_DIR)
