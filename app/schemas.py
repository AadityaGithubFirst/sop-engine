"""Pydantic request/response models for the SOP Generation Engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class StakeholderInput(BaseModel):
    """A single human or organisational actor attached to the project."""

    name: str = Field(..., min_length=1, description="Full name or unit name.")
    role: str = Field(..., min_length=1, description="Designation, e.g. 'Deputy Secretary'.")
    department: Optional[str] = Field(
        None, description="Owning department or directorate, if different from the project."
    )

    @field_validator("name", "role")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class ToolDetail(BaseModel):
    """User-supplied or user-approved description for a tool.

    Populated when a tool is not in the shipped catalog and the officer has
    accepted a drafted description or written their own. The text is carried
    into the generated document verbatim.
    """

    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = ""
    source: str = "user"          # catalog | local_model | web_lookup | user
    typical_version: str = ""


class ProjectPayload(BaseModel):
    """Minimal project input from which a full SOP is autonomously inferred."""

    project_name: str = Field(..., min_length=1)
    department: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    stakeholders: List[StakeholderInput] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    tool_details: List[ToolDetail] = Field(
        default_factory=list,
        description="Descriptions for tools not in the catalog; carried into section 4.",
    )
    security_classification: Optional[str] = "RESTRICTED / INTERNAL"
    model_override: Optional[str] = Field(
        None, description="Run this request against a specific local model instead of the default."
    )

    def detail_for(self, tool: str) -> Optional["ToolDetail"]:
        """Look up the supplied description for a tool, if there is one."""
        target = tool.casefold().strip()
        for detail in self.tool_details:
            if detail.name.casefold().strip() == target:
                return detail
        return None

    model_config = {
        "json_schema_extra": {
            "example": {
                "project_name": "National Land Records Digitisation",
                "department": "Department of Revenue & Land Reforms",
                "description": (
                    "Digitise legacy paper land records across 42 district offices, "
                    "validate ownership data, and publish a weekly compliance dashboard."
                ),
                "stakeholders": [
                    {
                        "name": "A. Krishnan",
                        "role": "Principal Secretary",
                        "department": "Revenue",
                    },
                    {
                        "name": "R. Mehta",
                        "role": "Data Operations Lead",
                        "department": "IT Cell",
                    },
                ],
                "tools": ["Python", "PostgreSQL", "PowerBI", "Docker"],
                "security_classification": "RESTRICTED / INTERNAL",
            }
        }
    }


class ValidationIssue(BaseModel):
    """A single defect found by the programmatic validation gate."""

    code: str
    severity: str = "error"          # "error" blocks release; "warning" does not
    gate: str = "structure"          # tool_coverage | raci | execution_depth | ...
    message: str
    repair_hint: Optional[str] = None


class ValidationReport(BaseModel):
    """Outcome of the post-generation validation gate."""

    passed: bool
    attempts: int = 1
    issues: List[ValidationIssue] = Field(default_factory=list)
    tools_declared: List[str] = Field(default_factory=list)
    tools_missing: List[str] = Field(default_factory=list)
    phases_found: List[int] = Field(default_factory=list)
    raci_rows_checked: int = 0

    @property
    def errors(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


class SOPResponse(BaseModel):
    """Successful generation result."""

    document_id: str
    markdown_content: str
    docx_download_url: str
    docx_path: Optional[str] = Field(
        None, description="Absolute path of the saved .docx on the server machine."
    )
    validation: Optional[ValidationReport] = None


class GenerationMetadata(BaseModel):
    """Non-essential telemetry returned alongside a generated document."""

    model_name: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tech_analysis_chars: int = 0
    governance_analysis_chars: int = 0
    operations_analysis_chars: int = 0
    offline: bool = True
    passes_executed: int = 3
    repair_attempts: int = 0


class HealthResponse(BaseModel):
    """Runtime readiness report for the local inference stack."""

    status: str
    app_version: str
    ollama_host: str
    model_name: str
    ollama_reachable: bool
    model_available: bool
    available_models: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    detail: str
    hint: Optional[str] = None


class ToolResearchRequest(BaseModel):
    """One iteration of the unknown-tool research loop."""

    name: str = Field(..., min_length=1)
    attempt: int = 1
    rejected: List[str] = Field(
        default_factory=list, description="Descriptions the officer has already rejected."
    )
    hint: str = Field("", description="What was wrong with the previous draft.")


class ToolAcceptRequest(BaseModel):
    """An accepted description, queued for administrator approval."""

    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = ""
    source: str = "user"
    submitted_by: str = "unattributed"


class PersonRememberRequest(BaseModel):
    """A person the user entered, saved so their name autocompletes next time."""

    name: str = Field(..., min_length=1)
    role: str = ""
    department: str = ""


class CatalogSubmitRequest(BaseModel):
    """A user-proposed department or designation."""

    kind: str = Field(..., description="tool | department | role")
    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = ""
    submitted_by: str = "unattributed"


class AdminDecisionRequest(BaseModel):
    """Administrator approval or rejection of a queued entry."""

    entry_id: str = Field(..., min_length=1)
    reason: str = ""
    decided_by: str = "administrator"


class WebLookupRequest(BaseModel):
    """Operator decision on whether the engine may consult public sites."""

    enabled: bool
    changed_by: str = "operator"
