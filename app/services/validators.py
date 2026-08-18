"""Programmatic post-generation validation gate.

Every check here runs in Python against the generated Markdown - no model is
consulted. A failing check produces a machine-readable `ValidationIssue` whose
`repair_hint` is fed back to the responsible agent for a retry pass.

Three gates are enforced, per the engine specification:

1. Tool array verification - `set(input_tools) ⊆ set(mentioned_tools)`, and
   every tool must own a dedicated sub-block in section 4.
2. RACI completeness - every activity row carries at least one `R` and exactly
   one `A`, and every named actor exists in the Stakeholder Register.
3. Execution depth - section 5 contains the four mandatory phases, each backed
   by concrete commands and explicit error handling.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set

from app.schemas import ProjectPayload, ValidationIssue, ValidationReport
from app.services.markdown_utils import (
    MarkdownTable,
    find_section,
    has_code_evidence,
    normalise,
    parse_sections,
    parse_tables,
)

# --- Mandatory execution phases (section 5) --------------------------------
# Each phase is identified by its number; the keyword sets prove the phase is
# about the right subject rather than a renamed placeholder.
MANDATORY_PHASES: Dict[int, Dict[str, object]] = {
    1: {
        "name": "Environment Setup & Health Checks",
        "keywords": {"environment", "setup", "health", "check", "credential", "connectivity"},
    },
    2: {
        "name": "Ingestion & Data Transformation",
        "keywords": {"ingest", "transform", "extract", "load file", "directory", "parse"},
    },
    3: {
        "name": "Database Load & Indexing",
        "keywords": {"database", "load", "index", "staging", "commit", "sql", "table"},
    },
    4: {
        "name": "Reporting, Dashboards & Export",
        "keywords": {"report", "dashboard", "export", "refresh", "publish", "sign-off"},
    },
}

# --- Mandatory technical failure-mode categories (section 4) ---------------
FAILURE_CATEGORIES: Dict[str, Set[str]] = {
    "data_corruption": {"corrupt", "schema", "validation", "malformed", "integrity", "checksum"},
    "connection_failure": {"timeout", "deadlock", "connection", "unreachable", "network", "refused"},
    "resource_exhaustion": {"disk", "memory", "ram", "oom", "resource", "exhaust", "crash", "container"},
    "downstream_sync": {"refresh", "sync", "downstream", "dashboard", "gateway", "publish", "report"},
}

# Cells such as "A", "R/A", "A (final)" all count toward their letters.
_RACI_LETTER_PATTERN = re.compile(r"\b([RACI])\b")

# Phrases that signal a step is directive rather than executable.
SHALLOW_STEP_PATTERNS = (
    re.compile(r"^\s*\d+\.\s*(verify|check|ensure|confirm|review|validate)\s+\w+\s*\.?\s*$", re.I),
)

ERROR_HANDLING_KEYWORDS = {
    "if the",
    "on failure",
    "if this fails",
    "error",
    "fails",
    "failure",
    "rollback",
    "retry",
    "abort",
    "fallback",
    "exit code",
    "escalate",
}


class SOPValidationError(RuntimeError):
    """Raised when a document cannot be brought up to standard.

    Carries the report so the API can tell the operator exactly which gate
    failed rather than returning an opaque 500.
    """

    def __init__(self, report: "ValidationReport") -> None:
        summary = "; ".join(issue.message for issue in report.errors) or "unknown failure"
        super().__init__(f"SOP failed structural validation: {summary}")
        self.report = report


# ---------------------------------------------------------------------------
# Gate 1 - tool coverage
# ---------------------------------------------------------------------------
def _tool_aliases(tool: str) -> Set[str]:
    """Accepted spellings for a tool name.

    Models routinely write "Power BI" for "PowerBI" or "Postgres" for
    "PostgreSQL"; treating those as misses would trigger pointless retries.
    """
    base = normalise(tool)
    aliases = {base, base.replace(" ", ""), base.replace("-", " ")}
    known = {
        "powerbi": {"power bi", "power-bi", "powerbi service", "power bi desktop"},
        "postgresql": {"postgres", "psql", "postgre sql"},
        "docker": {"docker engine", "docker compose", "container runtime"},
        "python": {"python3", "cpython"},
        "sql": {"sql server", "t-sql", "ansi sql"},
        "kubernetes": {"k8s"},
    }
    aliases |= known.get(base.replace(" ", ""), set())
    return {alias for alias in aliases if alias}


def find_missing_tools(tech_section: str, tools: Sequence[str]) -> List[str]:
    """Tools from the input array that are never mentioned in the section."""
    haystack = normalise(tech_section)
    return [tool for tool in tools if not any(alias in haystack for alias in _tool_aliases(tool))]


def find_tools_without_block(tech_section: str, tools: Sequence[str]) -> List[str]:
    """Tools lacking a dedicated `####` sub-block heading in section 4."""
    headings = [
        normalise(section.title)
        for level in (3, 4, 5)
        for section in parse_sections(tech_section, level=level)
    ]
    missing: List[str] = []
    for tool in tools:
        aliases = _tool_aliases(tool)
        if not any(any(alias in heading for alias in aliases) for heading in headings):
            missing.append(tool)
    return missing


def validate_tool_coverage(tech_section: str, tools: Sequence[str]) -> List[ValidationIssue]:
    """Gate 1: every declared tool is mentioned and owns a sub-block."""
    issues: List[ValidationIssue] = []
    active = [tool for tool in tools if tool and tool.strip()]
    if not active:
        return issues

    missing = find_missing_tools(tech_section, active)
    if missing:
        issues.append(
            ValidationIssue(
                code="tools_not_mentioned",
                severity="error",
                gate="tool_coverage",
                message=f"Section 4 omits declared tools: {', '.join(missing)}.",
                repair_hint=(
                    "Add a complete '#### <Tool>' sub-block for each of these tools: "
                    + ", ".join(missing)
                ),
            )
        )

    without_block = [tool for tool in find_tools_without_block(tech_section, active) if tool not in missing]
    if without_block:
        issues.append(
            ValidationIssue(
                code="tools_without_subblock",
                severity="error",
                gate="tool_coverage",
                message=(
                    "These tools are mentioned but have no dedicated sub-block: "
                    + ", ".join(without_block)
                ),
                repair_hint=(
                    "Give each of these tools its own '#### <Tool>' heading with version, "
                    "purpose, configuration, verification command, and failure notes: "
                    + ", ".join(without_block)
                ),
            )
        )
    return issues


def validate_failure_modes(tech_section: str) -> List[ValidationIssue]:
    """Section 4 must cover the four mandated failure categories."""
    issues: List[ValidationIssue] = []
    table = _find_failure_table(tech_section)
    row_count = len(table.rows) if table else 0

    if row_count < 4:
        issues.append(
            ValidationIssue(
                code="insufficient_failure_modes",
                severity="error",
                gate="failure_modes",
                message=f"Section 4 defines {row_count} failure modes; at least 4 are required.",
                repair_hint=(
                    "Produce a failure-mode table with at least four rows covering data "
                    "corruption/schema validation, connection timeout or deadlock, resource "
                    "exhaustion (disk/RAM/container crash), and downstream refresh failure."
                ),
            )
        )

    haystack = normalise(table_text(table) if table else tech_section)
    uncovered = [
        category
        for category, keywords in FAILURE_CATEGORIES.items()
        if not any(keyword in haystack for keyword in keywords)
    ]
    if uncovered:
        issues.append(
            ValidationIssue(
                code="failure_category_missing",
                severity="error",
                gate="failure_modes",
                message="Failure categories not covered: " + ", ".join(uncovered),
                repair_hint=(
                    "Add one failure-mode row for each missing category: "
                    + ", ".join(uncovered)
                ),
            )
        )
    return issues


def _find_failure_table(tech_section: str) -> Optional[MarkdownTable]:
    """The failure-mode table, identified by its column names."""
    for table in parse_tables(tech_section):
        header = normalise(" ".join(table.header))
        if "failure" in header and ("mitigation" in header or "detection" in header):
            return table
    return None


def table_text(table: Optional[MarkdownTable]) -> str:
    if table is None:
        return ""
    return " ".join(" ".join(row) for row in [table.header, *table.rows])


# ---------------------------------------------------------------------------
# Gate 2 - RACI completeness and entity integrity
# ---------------------------------------------------------------------------
def _find_raci_table(governance_section: str) -> Optional[MarkdownTable]:
    for table in parse_tables(governance_section):
        if "activity" in normalise(table.title):
            return table
    return None


def _find_register_table(governance_section: str) -> Optional[MarkdownTable]:
    for table in parse_tables(governance_section):
        header = normalise(" ".join(table.header))
        if "stakeholder" in header and ("designation" in header or "department" in header):
            return table
    return None


def _letters(cell: str) -> Set[str]:
    """RACI letters present in a cell, tolerating combined values like 'R/A'."""
    return set(_RACI_LETTER_PATTERN.findall(cell.upper()))


def validate_raci(governance_section: str, payload: ProjectPayload) -> List[ValidationIssue]:
    """Gate 2: structural completeness plus stakeholder entity integrity."""
    issues: List[ValidationIssue] = []

    raci = _find_raci_table(governance_section)
    if raci is None:
        return [
            ValidationIssue(
                code="raci_table_missing",
                severity="error",
                gate="raci",
                message="No RACI matrix found (expected a table whose first column is 'Activity').",
                repair_hint=(
                    "Emit a '### RACI Matrix' table whose first column is 'Activity' and "
                    "whose remaining columns are one per stakeholder."
                ),
            )
        ]

    incomplete: List[str] = []
    multiple_accountable: List[str] = []
    for row in raci.rows:
        activity = row[0].strip()
        if not activity or set(activity) <= {"-", " "}:
            continue
        letters: Set[str] = set()
        accountable_count = 0
        for cell in row[1:]:
            cell_letters = _letters(cell)
            letters |= cell_letters
            accountable_count += 1 if "A" in cell_letters else 0
        if not {"R", "A"} <= letters:
            missing = ", ".join(sorted({"R", "A"} - letters))
            incomplete.append(f"'{activity}' (missing {missing})")
        if accountable_count > 1:
            multiple_accountable.append(f"'{activity}' has {accountable_count} Accountable officers")

    if incomplete:
        issues.append(
            ValidationIssue(
                code="raci_row_incomplete",
                severity="error",
                gate="raci",
                message="RACI rows lacking an R or an A: " + "; ".join(incomplete),
                repair_hint=(
                    "Every activity row must assign exactly one 'A' and at least one 'R'. "
                    "Fix these rows: " + "; ".join(incomplete)
                ),
            )
        )
    if multiple_accountable:
        issues.append(
            ValidationIssue(
                code="raci_multiple_accountable",
                severity="error",
                gate="raci",
                message="; ".join(multiple_accountable),
                repair_hint=(
                    "Exactly one stakeholder may be Accountable per activity. Demote the "
                    "extra 'A' assignments to 'C' or 'R'."
                ),
            )
        )

    issues.extend(_validate_entity_integrity(governance_section, raci, payload))
    return issues


def _validate_entity_integrity(
    governance_section: str, raci: MarkdownTable, payload: ProjectPayload
) -> List[ValidationIssue]:
    """Every RACI actor must exist in the register, and vice versa for inputs."""
    issues: List[ValidationIssue] = []
    register = _find_register_table(governance_section)

    if register is None:
        return [
            ValidationIssue(
                code="register_missing",
                severity="error",
                gate="raci",
                message="No Stakeholder Register table found.",
                repair_hint=(
                    "Emit a '### Stakeholder Register' table with columns "
                    "| Stakeholder | Designation | Department | Governance Function |."
                ),
            )
        ]

    registered = {normalise(row[0]) for row in register.rows if row and row[0].strip()}
    actors = [header.strip() for header in raci.header[1:] if header.strip()]

    unregistered = [
        actor
        for actor in actors
        if not any(
            normalise(actor) == name or normalise(actor) in name or name in normalise(actor)
            for name in registered
        )
    ]
    if unregistered:
        issues.append(
            ValidationIssue(
                code="raci_actor_unregistered",
                severity="error",
                gate="raci",
                message=(
                    "RACI columns name actors absent from the Stakeholder Register: "
                    + ", ".join(unregistered)
                ),
                repair_hint=(
                    "Add a register row (Name, Designation, Department, Governance Role) "
                    "for each of: " + ", ".join(unregistered)
                ),
            )
        )

    supplied_missing = [
        person.name
        for person in payload.stakeholders
        if not any(normalise(person.name) in name or name in normalise(person.name) for name in registered)
    ]
    if supplied_missing:
        issues.append(
            ValidationIssue(
                code="stakeholder_dropped",
                severity="error",
                gate="raci",
                message="Supplied stakeholders missing from the register: " + ", ".join(supplied_missing),
                repair_hint=(
                    "Every stakeholder supplied in the request must appear in the register "
                    "and in the RACI matrix: " + ", ".join(supplied_missing)
                ),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Gate 3 - execution depth
# ---------------------------------------------------------------------------
def phase_blocks(operations_section: str) -> Dict[int, str]:
    """Map phase number -> phase body, for any `Phase N` heading."""
    blocks: Dict[int, str] = {}
    for level in (3, 4):
        for section in parse_sections(operations_section, level=level):
            match = re.search(r"phase\s*(\d+)", section.title, re.I)
            if match:
                number = int(match.group(1))
                blocks.setdefault(number, f"{section.title}\n{section.body}")
    return blocks


def validate_execution_depth(operations_section: str) -> List[ValidationIssue]:
    """Gate 3: four mandatory phases, each executable rather than directive."""
    issues: List[ValidationIssue] = []
    blocks = phase_blocks(operations_section)

    missing = [number for number in MANDATORY_PHASES if number not in blocks]
    if missing:
        names = ", ".join(f"Phase {n} ({MANDATORY_PHASES[n]['name']})" for n in missing)
        issues.append(
            ValidationIssue(
                code="phase_missing",
                severity="error",
                gate="execution_depth",
                message=f"Section 5 is missing mandatory phases: {names}.",
                repair_hint=(
                    "Section 5 must contain all four phases in order: "
                    + "; ".join(f"Phase {n} - {MANDATORY_PHASES[n]['name']}" for n in MANDATORY_PHASES)
                ),
            )
        )

    for number, body in sorted(blocks.items()):
        if number not in MANDATORY_PHASES:
            continue
        label = f"Phase {number} ({MANDATORY_PHASES[number]['name']})"

        if not has_code_evidence(body):
            issues.append(
                ValidationIssue(
                    code="phase_lacks_commands",
                    severity="error",
                    gate="execution_depth",
                    message=f"{label} contains no commands, queries, or endpoints.",
                    repair_hint=(
                        f"Rewrite {label} so every step carries an exact CLI command, SQL "
                        "query, or API endpoint in a fenced code block."
                    ),
                )
            )

        if not _has_error_handling(body):
            issues.append(
                ValidationIssue(
                    code="phase_lacks_error_handling",
                    severity="error",
                    gate="execution_depth",
                    message=f"{label} defines no failure handling or fallback step.",
                    repair_hint=(
                        f"Add an explicit error-handling step to {label}: what the operator "
                        "sees when it fails, the fallback command, and who is escalated to."
                    ),
                )
            )

        shallow = _shallow_steps(body)
        if shallow:
            issues.append(
                ValidationIssue(
                    code="phase_steps_shallow",
                    severity="warning",
                    gate="execution_depth",
                    message=f"{label} contains directive steps without detail: " + "; ".join(shallow[:3]),
                    repair_hint=(
                        "Replace high-level steps such as 'Verify access' with the exact "
                        "command run and the expected output."
                    ),
                )
            )
    return issues


def _has_error_handling(text: str) -> bool:
    body = normalise(text)
    return any(keyword in body for keyword in ERROR_HANDLING_KEYWORDS)


def _shallow_steps(text: str) -> List[str]:
    """Numbered steps that give an instruction but no command or detail."""
    found: List[str] = []
    for line in text.split("\n"):
        for pattern in SHALLOW_STEP_PATTERNS:
            if pattern.match(line) and not has_code_evidence(line):
                found.append(line.strip())
    return found


# ---------------------------------------------------------------------------
# Composite gates
# ---------------------------------------------------------------------------
def validate_governance_pass(section: str, payload: ProjectPayload) -> List[ValidationIssue]:
    """Pass 1 gate."""
    return validate_raci(section, payload)


def validate_tech_pass(section: str, payload: ProjectPayload) -> List[ValidationIssue]:
    """Pass 2 gate."""
    return [*validate_tool_coverage(section, payload.tools), *validate_failure_modes(section)]


def validate_operations_pass(section: str, _payload: ProjectPayload) -> List[ValidationIssue]:
    """Pass 3 gate."""
    return validate_execution_depth(section)


def validate_document(markdown: str, payload: ProjectPayload, attempts: int = 1) -> ValidationReport:
    """Final gate: re-run every check against the assembled document.

    Runs immediately before the `.docx` is written, so a document that passes
    here is the document that ships.
    """
    governance = find_section(markdown, "governance") or find_section(markdown, "raci")
    technical = find_section(markdown, "tooling") or find_section(markdown, "technical")
    operations = find_section(markdown, "execution") or find_section(markdown, "operating")

    governance_body = governance.body if governance else ""
    technical_body = technical.body if technical else ""
    operations_body = operations.body if operations else ""

    issues: List[ValidationIssue] = []
    if governance is None:
        issues.append(
            ValidationIssue(
                code="section_missing",
                severity="error",
                gate="structure",
                message="Section 3 (Governance and RACI) is absent from the document.",
                repair_hint="Regenerate the governance section.",
            )
        )
    if technical is None:
        issues.append(
            ValidationIssue(
                code="section_missing",
                severity="error",
                gate="structure",
                message="Section 4 (Tooling Architecture) is absent from the document.",
                repair_hint="Regenerate the technical section.",
            )
        )
    if operations is None:
        issues.append(
            ValidationIssue(
                code="section_missing",
                severity="error",
                gate="structure",
                message="Section 5 (Operating Execution Phases) is absent from the document.",
                repair_hint="Regenerate the operational section.",
            )
        )

    issues.extend(validate_governance_pass(governance_body, payload))
    issues.extend(validate_tech_pass(technical_body, payload))
    issues.extend(validate_operations_pass(operations_body, payload))

    active_tools = [tool for tool in payload.tools if tool and tool.strip()]
    missing_tools = find_missing_tools(technical_body, active_tools)
    phases_found = sorted(phase_blocks(operations_body))

    return ValidationReport(
        passed=not any(issue.severity == "error" for issue in issues),
        attempts=attempts,
        issues=issues,
        tools_declared=active_tools,
        tools_missing=missing_tools,
        phases_found=phases_found,
        raci_rows_checked=len(_find_raci_table(governance_body).rows)
        if _find_raci_table(governance_body)
        else 0,
    )


def summarise_hints(issues: Iterable[ValidationIssue]) -> str:
    """Render repair hints as a numbered list for a retry prompt."""
    hints = [issue.repair_hint for issue in issues if issue.repair_hint]
    return "\n".join(f"{index}. {hint}" for index, hint in enumerate(hints, start=1))
