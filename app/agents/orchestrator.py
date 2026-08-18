"""Master Orchestrator - three-pass sequential generation with validation gates.

No document is produced in a single LLM call. Each pass is generated, validated
programmatically, and re-prompted with explicit repair hints when it fails:

    Pass 1  Governance & Entity Integrity   -> section 3
    Pass 2  Technical Stack Matching        -> section 4
    Pass 3  Deep Operational Execution      -> sections 5 and 6

Passes run in order because pass 3 consumes the outputs of 1 and 2. After the
retry budget is spent, deterministic repair fills any remaining gap, so a
structurally valid document is always produced. A final gate re-validates the
assembled document before the `.docx` is written.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from app.agents import governance_agent, operations_agent, tech_agent
from app.agents.governance_agent import GovernanceAgent
from app.agents.operations_agent import OperationsAgent
from app.agents.tech_agent import TechAgent
from app.config import settings
from app.schemas import GenerationMetadata, ProjectPayload, ValidationIssue, ValidationReport
from app.services import validators
from app.services.ollama_client import (
    OllamaClient,
    OllamaUnavailableError,
    get_client,
    strip_reasoning,
)
from app.services.validators import SOPValidationError

logger = logging.getLogger(__name__)


def _document_id(payload: ProjectPayload) -> str:
    """Stable, human-readable document identifier.

    Format: SOP-<DEPT>-<YYYYMMDD>-<8 hex chars>. The suffix is random so two
    generations of the same project never collide on disk.
    """
    slug = "".join(ch for ch in payload.department.upper() if ch.isalnum())[:6] or "GOVT"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"SOP-{slug}-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _revision_hash(payload: ProjectPayload) -> str:
    """Content fingerprint of the inputs, for change detection across revisions."""
    basis = "|".join(
        [
            payload.project_name,
            payload.department,
            payload.description,
            ";".join(sorted(payload.tools)),
            ";".join(sorted(f"{s.name}:{s.role}" for s in payload.stakeholders)),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12].upper()


def _format_defects(issues: Sequence[ValidationIssue]) -> str:
    return "\n".join(f"- [{issue.gate}] {issue.message}" for issue in issues) or "- none"


class MasterOrchestrator:
    """Coordinates the three generation passes and the validation gates."""

    name = "MasterOrchestrator"

    def __init__(
        self,
        client: Optional[OllamaClient] = None,
        tech_agent_impl: Optional[TechAgent] = None,
        governance_agent_impl: Optional[GovernanceAgent] = None,
        operations_agent_impl: Optional[OperationsAgent] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.client = client or get_client()
        self.tech_agent = tech_agent_impl or TechAgent(self.client)
        self.governance_agent = governance_agent_impl or GovernanceAgent(self.client)
        self.operations_agent = operations_agent_impl or OperationsAgent(self.client)
        self.max_retries = (
            settings.MAX_VALIDATION_RETRIES if max_retries is None else max_retries
        )

    # -- public API ---------------------------------------------------------
    def generate(
        self, payload: ProjectPayload
    ) -> Tuple[str, str, GenerationMetadata, ValidationReport]:
        """Run all three passes and return the validated document.

        Returns `(document_id, markdown, metadata, validation_report)`. Raises
        `OllamaUnavailableError` if the local daemon is unreachable, and
        `SOPValidationError` if the assembled document still fails the gate
        after deterministic repair and `STRICT_VALIDATION` is on.
        """
        document_id = _document_id(payload)
        logger.info(
            "Generating %s for '%s' via 3-pass pipeline", document_id, payload.project_name
        )

        governance, governance_attempts = self._pass_one_governance(payload)
        technical, tech_attempts = self._pass_two_technical(payload)
        operations, ops_attempts = self._pass_three_operations(payload, technical, governance)

        markdown = self._assemble(
            payload=payload,
            document_id=document_id,
            governance=governance,
            technical=technical,
            operations=operations,
        )

        total_attempts = governance_attempts + tech_attempts + ops_attempts
        report = validators.validate_document(markdown, payload, attempts=total_attempts)

        if not report.passed:
            logger.error(
                "%s failed the final gate: %s",
                document_id,
                "; ".join(issue.message for issue in report.errors),
            )
            if settings.STRICT_VALIDATION:
                raise SOPValidationError(report)
        else:
            logger.info(
                "%s passed the final gate (%d generation attempts, %d warnings)",
                document_id,
                total_attempts,
                len(report.warnings),
            )

        metadata = GenerationMetadata(
            model_name=self.client.model,
            tech_analysis_chars=len(technical),
            governance_analysis_chars=len(governance),
            operations_analysis_chars=len(operations),
            passes_executed=3,
            repair_attempts=max(total_attempts - 3, 0),
        )
        return document_id, markdown, metadata, report

    # -- pass 1: governance & entity integrity ------------------------------
    def _pass_one_governance(self, payload: ProjectPayload) -> Tuple[str, int]:
        """Section 3, with RACI completeness and entity integrity enforced."""
        return self._run_validated_pass(
            label="pass-1-governance",
            generate=lambda: self.governance_agent.run(payload),
            repair=lambda previous, defects, hints: self.governance_agent.repair(
                payload, previous, defects, hints
            ),
            validate=lambda section: validators.validate_governance_pass(section, payload),
            deterministic_repair=lambda section, issues: self._repair_governance(
                payload, section, issues
            ),
            fallback=lambda: governance_agent.fallback_section(payload),
        )

    def _repair_governance(
        self, payload: ProjectPayload, section: str, issues: Sequence[ValidationIssue]
    ) -> str:
        """Rebuild the register/matrix deterministically when the model cannot."""
        logger.warning("Applying deterministic RACI repair (%d unresolved issues)", len(issues))
        return governance_agent.repair_raci(section, payload)

    # -- pass 2: technical stack matching -----------------------------------
    def _pass_two_technical(self, payload: ProjectPayload) -> Tuple[str, int]:
        """Section 4, with per-tool sub-blocks and mandated failure modes."""
        return self._run_validated_pass(
            label="pass-2-technical",
            generate=lambda: self.tech_agent.run(payload),
            repair=lambda previous, defects, hints: self.tech_agent.repair(
                payload, previous, defects, hints
            ),
            validate=lambda section: validators.validate_tech_pass(section, payload),
            deterministic_repair=lambda section, issues: self._repair_technical(
                payload, section, issues
            ),
            fallback=lambda: tech_agent.fallback_section(payload),
        )

    def _repair_technical(
        self, payload: ProjectPayload, section: str, issues: Sequence[ValidationIssue]
    ) -> str:
        """Fill omitted tool sub-blocks and the failure-mode table."""
        codes = {issue.code for issue in issues}
        active = [tool for tool in payload.tools if tool and tool.strip()]

        if {"tools_not_mentioned", "tools_without_subblock"} & codes:
            missing = sorted(
                set(validators.find_missing_tools(section, active))
                | set(validators.find_tools_without_block(section, active))
            )
            logger.warning("Deterministically adding tool sub-blocks: %s", ", ".join(missing))
            section = tech_agent.repair_missing_tools(section, missing, payload)

        if {"insufficient_failure_modes", "failure_category_missing"} & codes:
            logger.warning("Deterministically rebuilding the failure-mode table")
            section = tech_agent.repair_failure_modes(section)
        return section

    # -- pass 3: deep operational execution ---------------------------------
    def _pass_three_operations(
        self, payload: ProjectPayload, technical: str, governance: str
    ) -> Tuple[str, int]:
        """Sections 5 and 6, with the four mandatory phases and tactical depth."""
        return self._run_validated_pass(
            label="pass-3-operations",
            generate=lambda: self.operations_agent.run(payload, technical, governance),
            repair=lambda previous, defects, hints: self.operations_agent.repair(
                payload, previous, defects, hints
            ),
            validate=lambda section: validators.validate_operations_pass(section, payload),
            deterministic_repair=lambda section, issues: self._repair_operations(section, issues),
            fallback=lambda: operations_agent.fallback_section(payload),
        )

    def _repair_operations(self, section: str, issues: Sequence[ValidationIssue]) -> str:
        """Splice in any missing phase, and replace any phase too shallow to pass."""
        section = operations_agent.ensure_section_six(section)
        present = set(validators.phase_blocks(section))
        deficient = {
            number
            for number in validators.MANDATORY_PHASES
            for issue in issues
            if issue.code in {"phase_lacks_commands", "phase_lacks_error_handling"}
            and f"Phase {number}" in issue.message
        }
        replaceable = (set(validators.MANDATORY_PHASES) - present) | deficient
        if not replaceable:
            return section

        logger.warning(
            "Deterministically supplying phases: %s",
            ", ".join(str(number) for number in sorted(replaceable)),
        )
        # A phase present but too shallow is dropped first, so its full template
        # lands in the right position rather than duplicating a weak block.
        section = _drop_phases(section, sorted(deficient))
        return operations_agent.repair_missing_phases(section, sorted(replaceable))

    # -- shared pass machinery ----------------------------------------------
    def _run_validated_pass(
        self,
        label: str,
        generate: Callable[[], str],
        repair: Callable[[str, str, str], str],
        validate: Callable[[str], List[ValidationIssue]],
        deterministic_repair: Callable[[str, Sequence[ValidationIssue]], str],
        fallback: Callable[[], str],
    ) -> Tuple[str, int]:
        """Generate → validate → re-prompt, then deterministically repair.

        Returns `(section, attempts)`. A dead daemon propagates; any other
        exception falls back to the deterministic section rather than losing the
        document.
        """
        attempts = 0
        try:
            section = strip_reasoning(generate())
        except OllamaUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade, never lose the document
            logger.exception("%s failed outright; using fallback: %s", label, exc)
            return fallback(), 1
        attempts += 1

        issues = validate(section)
        errors = [issue for issue in issues if issue.severity == "error"]

        while errors and attempts <= self.max_retries:
            logger.warning(
                "%s attempt %d rejected by the gate: %s",
                label,
                attempts,
                "; ".join(issue.message for issue in errors),
            )
            try:
                section = strip_reasoning(
                    repair(section, _format_defects(errors), validators.summarise_hints(errors))
                )
            except OllamaUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s repair attempt failed: %s", label, exc)
                break
            attempts += 1
            issues = validate(section)
            errors = [issue for issue in issues if issue.severity == "error"]

        if errors:
            logger.warning(
                "%s still failing after %d attempts; applying deterministic repair",
                label,
                attempts,
            )
            section = deterministic_repair(section, errors)
            remaining = [issue for issue in validate(section) if issue.severity == "error"]
            if remaining:
                logger.error(
                    "%s deterministic repair insufficient; substituting full fallback section",
                    label,
                )
                section = fallback()
        else:
            logger.info("%s passed its gate on attempt %d", label, attempts)

        return section, attempts

    # -- document assembly --------------------------------------------------
    def _assemble(
        self,
        payload: ProjectPayload,
        document_id: str,
        governance: str,
        technical: str,
        operations: str,
    ) -> str:
        """Build the final Markdown document around the generated sections."""
        now = datetime.now(timezone.utc)
        issued = now.strftime("%d %B %Y")
        review_due = now.replace(year=now.year + 1).strftime("%d %B %Y")
        classification = payload.security_classification or "RESTRICTED / INTERNAL"
        tools = ", ".join(t.strip() for t in payload.tools if t.strip()) or "Not specified"

        return f"""# Standard Operating Procedure
## {payload.project_name}

**{classification}**

---

## 1. Document Control

| Field | Value |
| --- | --- |
| Document ID | `{document_id}` |
| Revision Hash | `{_revision_hash(payload)}` |
| Version | 1.0 (Draft for Approval) |
| Owning Department | {payload.department} |
| Security Classification | {classification} |
| Date of Issue | {issued} |
| Next Scheduled Review | {review_due} |
| Generated By | {settings.APP_NAME} v{settings.APP_VERSION} |
| Generation Method | 3-pass sequential agent pipeline with structural validation gates |
| Inference Runtime | Local Ollama - model `{self.client.model}` (air-gapped, no external data transfer) |
| Approval Status | Pending sign-off by the Accountable Officer |

> This document was assembled by an autonomous local inference engine and passed
> automated structural validation. It is a **draft for approval** and carries no
> authority until countersigned at the Release Gate defined in section 3.

---

## 2. Purpose and Scope

### 2.1 Purpose

This Standard Operating Procedure establishes the authorised, repeatable method
by which the {payload.department} executes **{payload.project_name}**. It exists
so that the activity is performed consistently, is auditable after the fact, and
survives changes in personnel.

### 2.2 Project Context

{payload.description.strip()}

### 2.3 Scope

This procedure applies to all officers, contractors, and delegated personnel of
the {payload.department} who perform, supervise, verify, or approve any activity
described herein, using the following tooling: {tools}.

### 2.4 Out of Scope

Activities outside the stated tooling and data boundary, procurement decisions,
and any transfer of data beyond the departmental network are **not** authorised
by this procedure and require separate approval.

### 2.5 Definitions

| Term | Definition |
| --- | --- |
| SOP | Standard Operating Procedure - the authorised method of performing an activity. |
| RACI | Responsible, Accountable, Consulted, Informed - the accountability model used in section 3. |
| Accountable Officer | The single officer answerable for the outcome of an activity. |
| Deviation | Any departure from the steps defined in this procedure. |
| Batch | One dated processing cycle, identified by its batch identifier. |
| Air-gapped | Executed wholly within the internal network, with no external data egress. |

---

## 3. Governance and RACI Stakeholder Matrix

{governance.strip()}

---

## 4. Tooling Architecture and Technical Prerequisites

{technical.strip()}

---

{operations.strip()}

---

## 7. Records, Retention and Document History

### 7.1 Records Maintained

| Record | Owner | Storage Location | Retention Period |
| --- | --- | --- | --- |
| Execution logs | Technical Lead | `/opt/sop/archive/YYYY-MM-DD/` | 3 years |
| Quarantined and rejected records | Data Operations Officer | `/opt/sop/quarantine/` | 3 years |
| Quality verification records | Quality Assurance Officer | Project records repository | 5 years |
| Approval and sign-off notes | Accountable Officer | Official records system | 7 years |
| Deviation reports | Compliance Reviewer | Project records repository | 7 years |

### 7.2 Revision History

| Version | Date | Author | Summary of Change |
| --- | --- | --- | --- |
| 1.0 | {issued} | {settings.APP_NAME} | Initial generation via 3-pass validated pipeline. |

### 7.3 Approval

| Role | Name | Signature | Date |
| --- | --- | --- | --- |
| Prepared By | | | |
| Reviewed By | | | |
| Approved By (Accountable Officer) | | | |

---

*End of document {document_id} - {classification}*
"""


def _drop_phases(section: str, numbers: Sequence[int]) -> str:
    """Remove the named phase blocks so full templates can replace them."""
    if not numbers:
        return section
    output: List[str] = []
    skipping = False
    for line in section.split("\n"):
        stripped = line.strip()
        if stripped.startswith("###"):
            skipping = any(f"phase {number}" in stripped.lower() for number in numbers)
        elif stripped.startswith("## "):
            skipping = False
        if not skipping:
            output.append(line)
    return "\n".join(output)
