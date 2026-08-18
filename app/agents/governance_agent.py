"""Pass 1 - Governance & Entity Integrity Agent.

Builds the Stakeholder Register and the RACI matrix under two hard invariants:

* Entity integrity - every actor named in a RACI column has a register entry
  (Name, Designation, Department, Governance Role).
* Row completeness - every activity carries exactly one `A` and at least one `R`.

Both are enforced programmatically by `app.services.validators`; failures are
re-prompted with explicit repair hints, then deterministically repaired if the
model still cannot comply.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from app.schemas import ProjectPayload, StakeholderInput
from app.services.ollama_client import OllamaClient, get_client

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """\
You are a Government Administrative Governance Officer authoring section 3
(Governance and RACI Stakeholder Matrix) of an official Standard Operating
Procedure.

RACI definitions:
- R (Responsible): performs the work.
- A (Accountable): the single final approver, answerable for the outcome.
- C (Consulted): two-way input required before the activity completes.
- I (Informed): one-way notification after the activity completes.

HARD RULES - a draft breaking any of these is rejected automatically:
1. ENTITY INTEGRITY. Every person or role named as a column in the RACI matrix
   MUST have a matching row in the Stakeholder Register, with all four fields:
   Name, Designation, Department, Governance Role. No actor may appear in the
   matrix without a register entry.
2. ROW COMPLETENESS. Every activity row MUST have exactly ONE 'A' and AT LEAST
   ONE 'R'. A row with no 'R', no 'A', or two 'A's is invalid.
3. Use the supplied stakeholders. Where a mandatory governance function has no
   named holder, add a ROLE-ONLY entry such as "Vacant - District Nodal Officer"
   to BOTH the register and the matrix, and flag it under Governance Gaps.
   Never invent a person's name.
4. Include district-level and field operational roles where the project has
   field execution: District Nodal Officer, Field Verification Officer, and the
   field data-entry function. These carry the 'R' on field activities.
5. The most senior supplied stakeholder is Accountable for authorisation and
   sign-off activities.
6. Never ask clarifying questions and never write "TBD" or "[insert value]".

Produce ONLY GitHub-flavoured Markdown starting at heading level 3 (###). No
document title, no preamble, no closing commentary.

Emit exactly these subsections, in order:

### Stakeholder Register
A table: | Stakeholder | Designation | Department | Governance Function |.
Include every supplied stakeholder, plus every district/field role you reference.

### RACI Matrix
A table whose first column header is exactly "Activity" and whose remaining
column headers are the stakeholder names EXACTLY as written in the register.
Each cell contains exactly one of R, A, C, I, or a hyphen.
Include at least nine activities covering: initiation and authorisation, data
access approval, district-level field data collection, technical execution,
data quality verification, security and compliance review, exception and
deviation handling, final sign-off, and records archival.

### Administrative Authority and Delegation
Who may authorise what, any operational limits, and the named delegate who acts
in the absence of the Accountable officer.

### Sign-off and Approval Gates
A numbered sequence of gates. For each: gate name, entry criteria, approving
authority, and the evidence recorded.

### Compliance Boundaries
Statutory, data-protection, records-retention, and information-security
obligations, and the specific action each requires of the operating team.

### Governance Gaps and Escalation
Vacant or missing governance functions, plus escalation levels 1 to 3 with the
trigger and response time for each.
"""

_USER_TEMPLATE = """\
Project Name: {project_name}
Owning Department: {department}
Security Classification: {classification}

Project Description:
{description}

Supplied Stakeholders ({count} - every one MUST appear in the register AND the matrix):
{stakeholders}

Map these stakeholders into a complete government governance and RACI structure.
Add district-level and field operational roles where the description implies
field execution.
"""

_REPAIR_TEMPLATE = """\
Your previous section 3 draft FAILED automated structural validation.

Project: {project_name} ({department})
Supplied Stakeholders: {stakeholder_list}

DEFECTS FOUND:
{defects}

REQUIRED CORRECTIONS:
{hints}

Rewrite section 3 IN FULL, correcting every defect. Remember: every RACI column
must have a Stakeholder Register row, and every activity row needs exactly one
'A' and at least one 'R'. Emit only the Markdown for section 3, starting at
'### Stakeholder Register'.

Your previous draft, for reference:
---
{previous}
---
"""

_DEFAULT_STAKEHOLDERS: List[StakeholderInput] = [
    StakeholderInput(name="Departmental Head", role="Accountable Officer", department=None),
    StakeholderInput(name="Project Lead", role="Responsible Officer", department=None),
    StakeholderInput(name="Records Officer", role="Compliance Reviewer", department=None),
]

# Role-only actors added when the supplied set cannot satisfy the RACI rules.
_FIELD_ROLES: List[StakeholderInput] = [
    StakeholderInput(
        name="Vacant - District Nodal Officer",
        role="District Nodal Officer",
        department="District Administration",
    ),
    StakeholderInput(
        name="Vacant - Field Verification Officer",
        role="Field Verification Officer",
        department="Field Operations",
    ),
]

ACTIVITIES = [
    "Project initiation and authorisation",
    "Data access approval",
    "District-level field data collection",
    "Technical execution and processing",
    "Data quality verification",
    "Security and compliance review",
    "Exception and deviation handling",
    "Final sign-off and release",
    "Records archival and retention",
]


def _resolve_stakeholders(payload: ProjectPayload) -> List[StakeholderInput]:
    """Supplied stakeholders, padded so an R and an A are always assignable.

    A single-stakeholder project cannot satisfy "one A plus at least one R"
    without a second actor, so field roles are appended as role-only entries.
    """
    people = list(payload.stakeholders) if payload.stakeholders else list(_DEFAULT_STAKEHOLDERS)
    if len(people) < 3:
        for role in _FIELD_ROLES:
            if len(people) >= 3:
                break
            people.append(role)
    return people


def _format_stakeholders(payload: ProjectPayload) -> str:
    lines = []
    for person in _resolve_stakeholders(payload):
        department = person.department or payload.department
        lines.append(f"- {person.name} | Designation: {person.role} | Department: {department}")
    return "\n".join(lines)


def build_prompt(payload: ProjectPayload) -> str:
    """Render the user-side prompt for the governance pass."""
    return _USER_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        classification=payload.security_classification or "RESTRICTED / INTERNAL",
        description=payload.description.strip(),
        stakeholders=_format_stakeholders(payload),
        count=len(_resolve_stakeholders(payload)),
    )


def build_repair_prompt(payload: ProjectPayload, previous: str, defects: str, hints: str) -> str:
    """Render the retry prompt naming every validation defect."""
    return _REPAIR_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        stakeholder_list=", ".join(person.name for person in _resolve_stakeholders(payload)),
        defects=defects,
        hints=hints,
        previous=previous[:6000],
    )


class GovernanceAgent:
    """Pass 1: governance and entity integrity."""

    name = "GovernanceRACIAgent"
    pass_number = 1

    def __init__(self, client: Optional[OllamaClient] = None) -> None:
        self.client = client or get_client()

    def run(self, payload: ProjectPayload) -> str:
        """First-attempt generation of section 3."""
        result = self.client.generate(
            system_prompt=SYSTEM_INSTRUCTION,
            user_prompt=build_prompt(payload),
        )
        return result if result.strip() else fallback_section(payload)

    def repair(self, payload: ProjectPayload, previous: str, defects: str, hints: str) -> str:
        """Retry generation with the validator's defects fed back in."""
        result = self.client.generate(
            system_prompt=SYSTEM_INSTRUCTION,
            user_prompt=build_repair_prompt(payload, previous, defects, hints),
        )
        return result if result.strip() else previous


# ---------------------------------------------------------------------------
# Deterministic repair helpers
# ---------------------------------------------------------------------------
def _assign(activity: str, index: int, total: int) -> str:
    """RACI letter for actor `index` on `activity`.

    Index 0 is Accountable throughout (single-A rule); index 1 always carries
    the R so no row can be left without one; the remainder are Consulted on
    review-type activities and Informed otherwise.
    """
    if index == 0:
        return "A"
    if index == 1:
        return "R"
    lowered = activity.lower()
    if any(word in lowered for word in ("field", "collection", "execution", "verification")):
        return "R" if index == 2 else "C"
    if any(word in lowered for word in ("review", "compliance", "deviation", "quality")):
        return "C"
    return "I"


def build_raci_tables(payload: ProjectPayload) -> str:
    """Register and matrix that satisfy entity integrity and row completeness."""
    people = _resolve_stakeholders(payload)

    register_rows = "\n".join(
        "| {name} | {role} | {dept} | {function} |".format(
            name=person.name,
            role=person.role,
            dept=person.department or payload.department,
            function=(
                "Accountable Officer"
                if index == 0
                else "Responsible Delivery Officer"
                if index == 1
                else "Field / Support Function"
            ),
        )
        for index, person in enumerate(people)
    )

    headers = " | ".join(person.name for person in people)
    divider = " | ".join("---" for _ in people)
    matrix_rows = "\n".join(
        "| " + activity + " | " + " | ".join(
            _assign(activity, index, len(people)) for index in range(len(people))
        ) + " |"
        for activity in ACTIVITIES
    )

    return f"""### Stakeholder Register

| Stakeholder | Designation | Department | Governance Function |
| --- | --- | --- | --- |
{register_rows}

### RACI Matrix

| Activity | {headers} |
| --- | {divider} |
{matrix_rows}"""


def fallback_section(payload: ProjectPayload) -> str:
    """Deterministic section 3 that satisfies every structural gate."""
    people = _resolve_stakeholders(payload)
    accountable = people[0].name
    responsible = people[1].name if len(people) > 1 else people[0].name
    classification = payload.security_classification or "RESTRICTED / INTERNAL"

    return f"""{build_raci_tables(payload)}

### Administrative Authority and Delegation

{accountable} holds final accountability for this procedure and is the sole
approving authority at every gate below. Day-to-day operational decisions are
delegated to {responsible}, who may authorise routine re-runs and exception
handling within the agreed processing window without further approval.

In the absence of {accountable}, authority passes to the next most senior
officer in the Stakeholder Register. Every delegated decision is recorded in the
project register with the date, the decision, and the officer acting.

District-level activity is directed by the District Nodal Officer, who is
Responsible for field data collection and reports operational exceptions to
{responsible} within one working day.

### Sign-off and Approval Gates

1. **Initiation Gate** - entry: approved project brief and confirmed funding
   line; authority: {accountable}; evidence: signed authorisation note filed in
   the official records system.
2. **Execution Readiness Gate** - entry: tooling provisioned, access granted,
   and the health check in Phase 1 passed; authority: {accountable}; evidence:
   completed readiness checklist.
3. **Quality Gate** - entry: reconciliation complete with no unresolved critical
   exceptions; authority: {accountable}; evidence: quality verification record.
4. **Release Gate** - entry: compliance review cleared and the dashboard refresh
   verified; authority: {accountable}; evidence: countersigned release note.

### Compliance Boundaries

- Data is handled at the **{classification}** classification and must not leave
  the departmental network boundary under any circumstance.
- Personal data is processed on a lawful basis, minimised to the fields required
  for the stated purpose, and never copied to personal devices or external mail.
- Records are retained per the departmental records-retention schedule set out
  in section 7, and disposal requires written authorisation.
- Access is granted on a least-privilege, need-to-know basis and is reviewed at
  the start of every processing cycle.
- Any suspected breach is reported to the departmental compliance authority
  immediately and never later than the same working day.

### Governance Gaps and Escalation

- Any stakeholder shown as "Vacant" above is an open governance gap and **must
  be filled before the Release Gate is cleared.**
- Escalation Level 1 - operational deviation: raised to {responsible}; response
  within one working day.
- Escalation Level 2 - unresolved or repeat deviation: raised to {accountable};
  response within three working days.
- Escalation Level 3 - compliance or security breach: raised immediately to the
  departmental compliance authority; response same working day.
"""


def repair_raci(section: str, payload: ProjectPayload) -> str:
    """Replace a defective register/matrix pair with compliant tables.

    Preserves the narrative subsections the model produced (authority, gates,
    compliance, gaps), since those are rarely the cause of a gate failure.
    """
    tables = build_raci_tables(payload)
    marker = "### Administrative Authority"
    if marker in section:
        _, _, tail = section.partition(marker)
        return f"{tables}\n\n{marker}{tail}"
    return fallback_section(payload)
