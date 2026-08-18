"""End-to-end test suite for the SOP Generation Engine.

Every test runs fully offline: the Ollama client is replaced with a fake that
returns canned Markdown, so the suite passes on a machine where Ollama has
never been installed.

Coverage is organised around the three-pass architecture:
  Pass 1 governance/entity integrity, Pass 2 tool coverage, Pass 3 execution
  depth, plus the post-generation validation gate and its repair paths.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents import governance_agent, operations_agent, tech_agent
from app.agents.governance_agent import GovernanceAgent
from app.agents.operations_agent import OperationsAgent
from app.agents.orchestrator import MasterOrchestrator
from app.agents.tech_agent import TechAgent
from app.main import app
from app.schemas import ProjectPayload, StakeholderInput
from app.services import validators
from app.services.document_exporter import (
    export_markdown_to_docx,
    markdown_to_document,
    resolve_docx_path,
)
from app.services.markdown_utils import parse_sections, parse_tables
from app.services.ollama_client import (
    OllamaClient,
    OllamaUnavailableError,
    strip_reasoning,
)
from app.services.validators import SOPValidationError

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD = {
    "project_name": "National Land Records Digitisation",
    "department": "Department of Revenue and Land Reforms",
    "description": (
        "Digitise legacy paper land records across 42 district offices, validate "
        "ownership data against the revenue register, and publish a weekly "
        "compliance dashboard for district collectors."
    ),
    "stakeholders": [
        {"name": "A. Krishnan", "role": "Principal Secretary", "department": "Revenue"},
        {"name": "R. Mehta", "role": "Data Operations Lead", "department": "IT Cell"},
        {"name": "S. Iyer", "role": "Compliance Auditor", "department": "Audit"},
    ],
    "tools": ["Python", "PostgreSQL", "PowerBI", "Docker"],
    "security_classification": "RESTRICTED / INTERNAL",
}

# A deliberately defective technical section: PowerBI and Docker are missing,
# and the failure table has too few rows.
BAD_TECH_OUTPUT = """### Operating Environment

Ubuntu 22.04 servers on the departmental LAN.

### Tool Sub-Blocks

#### Python

- **Version:** 3.11

#### PostgreSQL

- **Version:** 15

### Failure Modes and Mitigations

| # | Failure Mode | Likely Trigger | Detection Signal | Immediate Mitigation | Escalation Owner |
| --- | --- | --- | --- | --- | --- |
| 1 | Schema validation failure | Upstream change | Validation error | Quarantine batch | Technical Lead |
"""

# A defective governance section: row without an R, and an actor missing from
# the register.
BAD_GOVERNANCE_OUTPUT = """### Stakeholder Register

| Stakeholder | Designation | Department | Governance Function |
| --- | --- | --- | --- |
| A. Krishnan | Principal Secretary | Revenue | Accountable Officer |

### RACI Matrix

| Activity | A. Krishnan | R. Mehta |
| --- | --- | --- |
| Project initiation and authorisation | A | I |
| Final sign-off | A | C |

### Administrative Authority and Delegation

The Principal Secretary approves.
"""

# A defective operations section: stops at Phase 1, no commands.
BAD_OPERATIONS_OUTPUT = """## 5. Operating Execution Phases

### Phase 1 - Preparation

**Objective:** Get ready.

1. Verify access.
2. Check the database.

## 6. Quality Assurance and Controls

### Audit Trail Requirements

- Log everything.
"""


def good_tech_output(payload: ProjectPayload | None = None) -> str:
    """A technical section that passes the gate, built from the real templates."""
    return tech_agent.fallback_section(ProjectPayload(**SAMPLE_PAYLOAD) if payload is None else payload)


def good_governance_output(payload: ProjectPayload | None = None) -> str:
    return governance_agent.fallback_section(
        ProjectPayload(**SAMPLE_PAYLOAD) if payload is None else payload
    )


def good_operations_output(payload: ProjectPayload | None = None) -> str:
    return operations_agent.fallback_section(
        ProjectPayload(**SAMPLE_PAYLOAD) if payload is None else payload
    )


class FakeOllamaClient:
    """Stand-in for `OllamaClient` returning canned sections keyed by agent.

    Matching is done on a marker phrase unique to each agent's system prompt,
    so the fake routes exactly as the real orchestrator would.
    """

    MARKERS = {
        "Systems Engineer": "tech",
        "Governance Officer": "governance",
        "Chief Procedure Officer": "operations",
    }

    def __init__(self, responses: dict[str, str] | None = None, fail: bool = False) -> None:
        self.model = "fake-model:test"
        self.host = "http://localhost:11434"
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.responses = responses or {
            "tech": good_tech_output(),
            "governance": good_governance_output(),
            "operations": good_operations_output(),
        }

    def _route(self, system_prompt: str) -> str:
        for marker, key in self.MARKERS.items():
            if marker in system_prompt:
                return key
        return "unknown"

    def generate(self, system_prompt: str, user_prompt: str, **_kwargs) -> str:
        if self.fail:
            raise OllamaUnavailableError("simulated daemon outage")
        key = self._route(system_prompt)
        self.calls.append((key, user_prompt))
        value = self.responses.get(key, "### Generic Section\n\nContent.")
        if callable(value):
            return value(len([c for c in self.calls if c[0] == key]))
        return value

    def health(self) -> dict:
        return {
            "ollama_reachable": True,
            "model_available": True,
            "available_models": [self.model],
            "detail": None,
        }

    def calls_for(self, key: str) -> list[str]:
        return [prompt for agent, prompt in self.calls if agent == key]


@pytest.fixture
def payload() -> ProjectPayload:
    return ProjectPayload(**SAMPLE_PAYLOAD)


@pytest.fixture
def fake_client() -> FakeOllamaClient:
    return FakeOllamaClient()


@pytest.fixture
def orchestrator(fake_client: FakeOllamaClient) -> MasterOrchestrator:
    return MasterOrchestrator(client=fake_client)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_payload_parses_sample(self, payload: ProjectPayload) -> None:
        assert payload.project_name == "National Land Records Digitisation"
        assert len(payload.stakeholders) == 3
        assert payload.tools == ["Python", "PostgreSQL", "PowerBI", "Docker"]

    def test_security_classification_defaults(self) -> None:
        minimal = ProjectPayload(
            project_name="P", department="D", description="X", stakeholders=[], tools=[]
        )
        assert minimal.security_classification == "RESTRICTED / INTERNAL"

    def test_stakeholder_department_optional(self) -> None:
        assert StakeholderInput(name="J. Doe", role="Officer").department is None

    def test_blank_stakeholder_name_rejected(self) -> None:
        with pytest.raises(Exception):
            StakeholderInput(name="   ", role="Officer")

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(Exception):
            ProjectPayload(department="D", description="X")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

class TestOllamaClient:
    def test_strip_reasoning_removes_think_block(self) -> None:
        assert strip_reasoning("<think>deliberation</think>\n## Real") == "## Real"

    def test_strip_reasoning_passthrough(self) -> None:
        assert strip_reasoning("## Plain") == "## Plain"

    def test_strip_reasoning_handles_unterminated_block(self) -> None:
        assert strip_reasoning("Visible<think>dangling") == "Visible"

    def test_health_reports_unreachable_daemon(self, monkeypatch) -> None:
        client = OllamaClient(host="http://127.0.0.1:1")
        monkeypatch.setattr(client, "list_models", lambda: [])
        report = client.health()
        assert report["ollama_reachable"] is False
        assert "Ollama daemon" in (report["detail"] or "")

    def test_health_matches_model_by_base_name(self, monkeypatch) -> None:
        client = OllamaClient(model="llama3.1:8b")
        monkeypatch.setattr(client, "list_models", lambda: ["llama3.1:latest"])
        assert client.health()["model_available"] is True

    def test_unavailable_error_carries_hint(self) -> None:
        assert "ollama" in OllamaUnavailableError("down").hint.lower()


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------

class TestMarkdownUtils:
    def test_parses_table(self) -> None:
        tables = parse_tables("| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert len(tables) == 1
        assert tables[0].header == ["A", "B"]
        assert tables[0].rows == [["1", "2"]]

    def test_ignores_tables_inside_code_fences(self) -> None:
        markdown = "```\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```"
        assert parse_tables(markdown) == []

    def test_headings_inside_fences_do_not_split_sections(self) -> None:
        markdown = "## Real\n\n```bash\n# not a heading\necho hi\n```\n\ntext"
        sections = parse_sections(markdown, level=2)
        assert len(sections) == 1
        assert "echo hi" in sections[0].body


# ---------------------------------------------------------------------------
# Gate 1 - tool coverage
# ---------------------------------------------------------------------------

class TestToolCoverageGate:
    def test_detects_omitted_tools(self, payload: ProjectPayload) -> None:
        issues = validators.validate_tool_coverage(BAD_TECH_OUTPUT, payload.tools)
        codes = {issue.code for issue in issues}
        assert "tools_not_mentioned" in codes
        message = " ".join(issue.message for issue in issues)
        assert "PowerBI" in message and "Docker" in message

    def test_subset_check_passes_on_complete_section(self, payload: ProjectPayload) -> None:
        assert validators.validate_tool_coverage(good_tech_output(), payload.tools) == []

    def test_every_tool_gets_a_subblock(self, payload: ProjectPayload) -> None:
        section = good_tech_output()
        assert validators.find_tools_without_block(section, payload.tools) == []

    def test_alias_spellings_accepted(self) -> None:
        section = "#### Power BI\n\nDetails.\n\n#### Postgres\n\nDetails."
        assert validators.find_missing_tools(section, ["PowerBI", "PostgreSQL"]) == []

    def test_mentioned_without_block_is_flagged(self) -> None:
        section = "### Operating Environment\n\nWe use Docker heavily.\n"
        codes = {i.code for i in validators.validate_tool_coverage(section, ["Docker"])}
        assert "tools_without_subblock" in codes

    def test_empty_tool_array_is_not_an_error(self) -> None:
        assert validators.validate_tool_coverage("### Anything", []) == []


class TestFailureModeGate:
    def test_detects_too_few_failure_modes(self) -> None:
        codes = {i.code for i in validators.validate_failure_modes(BAD_TECH_OUTPUT)}
        assert "insufficient_failure_modes" in codes

    def test_all_four_categories_required(self) -> None:
        issues = validators.validate_failure_modes(BAD_TECH_OUTPUT)
        message = " ".join(i.message for i in issues)
        assert "resource_exhaustion" in message or "downstream_sync" in message

    def test_compliant_table_passes(self) -> None:
        section = "### Failure Modes and Mitigations\n\n" + tech_agent.failure_mode_table()
        assert validators.validate_failure_modes(section) == []


# ---------------------------------------------------------------------------
# Gate 2 - RACI completeness and entity integrity
# ---------------------------------------------------------------------------

class TestRaciGate:
    def test_row_without_responsible_is_rejected(self, payload: ProjectPayload) -> None:
        codes = {i.code for i in validators.validate_raci(BAD_GOVERNANCE_OUTPUT, payload)}
        assert "raci_row_incomplete" in codes

    def test_actor_missing_from_register_is_rejected(self, payload: ProjectPayload) -> None:
        issues = validators.validate_raci(BAD_GOVERNANCE_OUTPUT, payload)
        codes = {issue.code for issue in issues}
        assert "raci_actor_unregistered" in codes
        assert any("R. Mehta" in issue.message for issue in issues)

    def test_dropped_stakeholder_is_rejected(self, payload: ProjectPayload) -> None:
        issues = validators.validate_raci(BAD_GOVERNANCE_OUTPUT, payload)
        assert any(issue.code == "stakeholder_dropped" for issue in issues)

    def test_two_accountable_officers_rejected(self, payload: ProjectPayload) -> None:
        section = (
            "### Stakeholder Register\n\n"
            "| Stakeholder | Designation | Department | Governance Function |\n"
            "| --- | --- | --- | --- |\n"
            "| A. Krishnan | Secretary | Revenue | Accountable |\n"
            "| R. Mehta | Lead | IT | Delivery |\n"
            "| S. Iyer | Auditor | Audit | Review |\n\n"
            "### RACI Matrix\n\n"
            "| Activity | A. Krishnan | R. Mehta | S. Iyer |\n"
            "| --- | --- | --- | --- |\n"
            "| Final sign-off | A | A | R |\n"
        )
        codes = {i.code for i in validators.validate_raci(section, payload)}
        assert "raci_multiple_accountable" in codes

    def test_missing_matrix_is_rejected(self, payload: ProjectPayload) -> None:
        codes = {i.code for i in validators.validate_raci("### Nothing here", payload)}
        assert "raci_table_missing" in codes

    def test_generated_matrix_passes(self, payload: ProjectPayload) -> None:
        assert validators.validate_raci(good_governance_output(), payload) == []

    def test_every_row_has_exactly_one_a_and_at_least_one_r(self, payload: ProjectPayload) -> None:
        section = governance_agent.build_raci_tables(payload)
        matrix = [t for t in parse_tables(section) if t.title.lower() == "activity"][0]
        assert matrix.rows, "expected activity rows"
        for row in matrix.rows:
            cells = [cell.strip().upper() for cell in row[1:]]
            assert cells.count("A") == 1, f"row must have exactly one A: {row[0]}"
            assert "R" in cells, f"row must have at least one R: {row[0]}"

    def test_single_stakeholder_still_yields_r_and_a(self) -> None:
        """A one-person project cannot satisfy R+A without added field roles."""
        solo = ProjectPayload(
            project_name="P",
            department="D",
            description="X",
            stakeholders=[StakeholderInput(name="Sole Officer", role="Head")],
            tools=[],
        )
        section = governance_agent.fallback_section(solo)
        assert validators.validate_raci(section, solo) == []

    def test_district_and_field_roles_present(self, payload: ProjectPayload) -> None:
        section = good_governance_output()
        assert "District" in section or "Field" in section


# ---------------------------------------------------------------------------
# Gate 3 - execution depth
# ---------------------------------------------------------------------------

class TestExecutionDepthGate:
    def test_document_ending_at_phase_one_is_rejected(self) -> None:
        issues = validators.validate_execution_depth(BAD_OPERATIONS_OUTPUT)
        codes = {issue.code for issue in issues}
        assert "phase_missing" in codes
        assert any("Phase 2" in issue.message for issue in issues)

    def test_phase_without_commands_is_rejected(self) -> None:
        codes = {i.code for i in validators.validate_execution_depth(BAD_OPERATIONS_OUTPUT)}
        assert "phase_lacks_commands" in codes

    def test_phase_without_error_handling_is_rejected(self) -> None:
        codes = {i.code for i in validators.validate_execution_depth(BAD_OPERATIONS_OUTPUT)}
        assert "phase_lacks_error_handling" in codes

    def test_shallow_step_raises_warning(self) -> None:
        issues = validators.validate_execution_depth(BAD_OPERATIONS_OUTPUT)
        shallow = [i for i in issues if i.code == "phase_steps_shallow"]
        assert shallow and shallow[0].severity == "warning"

    def test_all_four_phases_present_in_template(self) -> None:
        section = good_operations_output()
        assert sorted(validators.phase_blocks(section)) == [1, 2, 3, 4]

    def test_generated_phases_pass_the_gate(self) -> None:
        assert [
            issue
            for issue in validators.validate_execution_depth(good_operations_output())
            if issue.severity == "error"
        ] == []

    def test_each_phase_carries_commands_and_error_handling(self) -> None:
        blocks = validators.phase_blocks(good_operations_output())
        for number, body in blocks.items():
            assert "```" in body, f"Phase {number} needs fenced commands"
            assert "Error Handling" in body, f"Phase {number} needs error handling"

    def test_phases_contain_sql_and_endpoints(self) -> None:
        section = good_operations_output()
        assert "ON CONFLICT" in section          # primary-key collision handling
        assert "ROLLBACK" in section             # commit/rollback logic
        assert "http://" in section              # API endpoint
        assert "/opt/sop/data/incoming" in section  # directory path


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TestAgents:
    def test_tech_prompt_lists_every_tool(
        self, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        TechAgent(fake_client).run(payload)
        prompt = fake_client.calls_for("tech")[-1]
        for tool in payload.tools:
            assert tool in prompt

    def test_governance_prompt_lists_every_stakeholder(
        self, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        GovernanceAgent(fake_client).run(payload)
        prompt = fake_client.calls_for("governance")[-1]
        for person in payload.stakeholders:
            assert person.name in prompt

    def test_operations_prompt_includes_upstream_passes(
        self, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        OperationsAgent(fake_client).run(payload, "TECH-MARKER", "GOV-MARKER")
        prompt = fake_client.calls_for("operations")[-1]
        assert "TECH-MARKER" in prompt and "GOV-MARKER" in prompt

    def test_repair_prompt_carries_defects_and_hints(
        self, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        TechAgent(fake_client).repair(payload, "old draft", "- missing Docker", "1. Add Docker")
        prompt = fake_client.calls_for("tech")[-1]
        assert "missing Docker" in prompt and "Add Docker" in prompt and "old draft" in prompt

    def test_empty_output_falls_back(self, payload: ProjectPayload) -> None:
        blank = FakeOllamaClient(responses={"tech": "   "})
        assert "Tool Sub-Blocks" in TechAgent(blank).run(payload)

    def test_tool_block_helper_is_self_contained(self) -> None:
        block = tech_agent.tool_block("Docker")
        assert block.startswith("#### Docker")
        assert "```bash" in block and "Verification" in block

    def test_unknown_tool_still_produces_usable_block(self) -> None:
        block = tech_agent.tool_block("Tally ERP")
        assert "#### Tally ERP" in block and "```bash" in block

    def test_repairs_compose_without_discarding_each_other(self) -> None:
        """Regression: the failure-table repair used to delete added tool blocks."""
        repaired = tech_agent.repair_missing_tools(BAD_TECH_OUTPUT, ["PowerBI", "Docker"])
        repaired = tech_agent.repair_failure_modes(repaired)
        assert "#### PowerBI" in repaired and "#### Docker" in repaired
        assert validators.validate_tool_coverage(repaired, ["Python", "PostgreSQL", "PowerBI", "Docker"]) == []
        assert validators.validate_failure_modes(repaired) == []

    def test_failure_repair_preserves_following_subsections(self) -> None:
        section = "\n".join([
            "### Failure Modes and Mitigations",
            "",
            "| # | Failure Mode | Detection Signal | Immediate Mitigation |",
            "| --- | --- | --- | --- |",
            "| 1 | Something | Signal | Fix |",
            "",
            "### Technical Assumptions",
            "",
            "- An assumption worth keeping.",
        ])
        repaired = tech_agent.repair_failure_modes(section)
        assert "An assumption worth keeping." in repaired
        assert validators.validate_failure_modes(repaired) == []


# ---------------------------------------------------------------------------
# Three-pass orchestration
# ---------------------------------------------------------------------------

class TestMultiPassOrchestration:
    def test_three_separate_llm_passes_are_made(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        orchestrator.generate(payload)
        assert len(fake_client.calls_for("governance")) >= 1
        assert len(fake_client.calls_for("tech")) >= 1
        assert len(fake_client.calls_for("operations")) >= 1
        assert len(fake_client.calls) >= 3, "the document must not be one-shot"

    def test_passes_run_in_order(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload, fake_client: FakeOllamaClient
    ) -> None:
        orchestrator.generate(payload)
        order = [agent for agent, _ in fake_client.calls]
        assert order.index("governance") < order.index("tech") < order.index("operations")

    def test_all_mandatory_sections_present(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload
    ) -> None:
        _id, markdown, _meta, report = orchestrator.generate(payload)
        for heading in (
            "## 1. Document Control",
            "## 2. Purpose and Scope",
            "## 3. Governance and RACI Stakeholder Matrix",
            "## 4. Tooling Architecture and Technical Prerequisites",
            "## 5. Operating Execution Phases",
            "## 6. Quality Assurance and Controls",
            "## 7. Records, Retention and Document History",
        ):
            assert heading in markdown, f"missing section: {heading}"
        assert report.passed

    def test_final_report_summarises_the_gates(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload
    ) -> None:
        _id, _md, _meta, report = orchestrator.generate(payload)
        assert report.tools_declared == payload.tools
        assert report.tools_missing == []
        assert report.phases_found == [1, 2, 3, 4]
        assert report.raci_rows_checked >= 7

    def test_document_id_format(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload
    ) -> None:
        document_id, _md, _meta, _r = orchestrator.generate(payload)
        assert re.fullmatch(r"SOP-[A-Z0-9]{1,6}-\d{8}-[0-9A-F]{8}", document_id)

    def test_document_ids_are_unique(
        self, orchestrator: MasterOrchestrator, payload: ProjectPayload
    ) -> None:
        first, _a, _b, _c = orchestrator.generate(payload)
        second, _d, _e, _f = orchestrator.generate(payload)
        assert first != second

    def test_daemon_outage_propagates(self, payload: ProjectPayload) -> None:
        with pytest.raises(OllamaUnavailableError):
            MasterOrchestrator(client=FakeOllamaClient(fail=True)).generate(payload)

    def test_no_reasoning_leaks_into_document(self, payload: ProjectPayload) -> None:
        noisy = FakeOllamaClient(
            responses={
                "tech": "<think>hidden</think>" + good_tech_output(),
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        _id, markdown, _meta, _r = MasterOrchestrator(client=noisy).generate(payload)
        assert "<think>" not in markdown


# ---------------------------------------------------------------------------
# Retry and repair behaviour
# ---------------------------------------------------------------------------

class TestRetryAndRepair:
    def test_bad_tech_output_triggers_a_retry_pass(self, payload: ProjectPayload) -> None:
        client = FakeOllamaClient(
            responses={
                "tech": BAD_TECH_OUTPUT,
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        MasterOrchestrator(client=client).generate(payload)
        assert len(client.calls_for("tech")) > 1, "gate failure must re-prompt the agent"

    def test_retry_prompt_names_the_missing_tools(self, payload: ProjectPayload) -> None:
        client = FakeOllamaClient(
            responses={
                "tech": BAD_TECH_OUTPUT,
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        MasterOrchestrator(client=client).generate(payload)
        retry_prompt = client.calls_for("tech")[1]
        assert "FAILED automated structural validation" in retry_prompt
        assert "PowerBI" in retry_prompt and "Docker" in retry_prompt

    def test_persistent_bad_tech_is_repaired_deterministically(
        self, payload: ProjectPayload
    ) -> None:
        """The model never complies, so Python fills the gaps and the doc ships."""
        client = FakeOllamaClient(
            responses={
                "tech": BAD_TECH_OUTPUT,
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        _id, markdown, _meta, report = MasterOrchestrator(client=client).generate(payload)
        assert report.passed
        assert report.tools_missing == []
        assert "#### PowerBI" in markdown and "#### Docker" in markdown

    def test_persistent_bad_raci_is_repaired_deterministically(
        self, payload: ProjectPayload
    ) -> None:
        client = FakeOllamaClient(
            responses={
                "tech": good_tech_output(),
                "governance": BAD_GOVERNANCE_OUTPUT,
                "operations": good_operations_output(),
            }
        )
        _id, markdown, _meta, report = MasterOrchestrator(client=client).generate(payload)
        assert report.passed
        assert "S. Iyer" in markdown, "dropped stakeholder must be restored"

    def test_truncated_phases_are_repaired_deterministically(
        self, payload: ProjectPayload
    ) -> None:
        client = FakeOllamaClient(
            responses={
                "tech": good_tech_output(),
                "governance": good_governance_output(),
                "operations": BAD_OPERATIONS_OUTPUT,
            }
        )
        _id, markdown, _meta, report = MasterOrchestrator(client=client).generate(payload)
        assert report.passed
        assert report.phases_found == [1, 2, 3, 4]
        assert "ON CONFLICT" in markdown

    def test_retry_budget_is_respected(self, payload: ProjectPayload) -> None:
        client = FakeOllamaClient(
            responses={
                "tech": BAD_TECH_OUTPUT,
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        MasterOrchestrator(client=client, max_retries=1).generate(payload)
        assert len(client.calls_for("tech")) == 2  # initial attempt + one retry

    def test_recovery_on_second_attempt_stops_retrying(self, payload: ProjectPayload) -> None:
        """A model that fixes itself must not be prompted a third time."""
        good = good_tech_output()
        client = FakeOllamaClient(
            responses={
                "tech": lambda call_number: BAD_TECH_OUTPUT if call_number == 1 else good,
                "governance": good_governance_output(),
                "operations": good_operations_output(),
            }
        )
        _id, _md, _meta, report = MasterOrchestrator(client=client).generate(payload)
        assert len(client.calls_for("tech")) == 2
        assert report.passed

    def test_strict_validation_rejects_unfixable_document(
        self, payload: ProjectPayload, monkeypatch
    ) -> None:
        """If assembly itself is broken, the gate refuses to release the file."""
        orchestrator = MasterOrchestrator(client=FakeOllamaClient())
        monkeypatch.setattr(
            orchestrator, "_assemble", lambda **_kwargs: "# Broken\n\nNo sections here."
        )
        with pytest.raises(SOPValidationError) as excinfo:
            orchestrator.generate(payload)
        assert excinfo.value.report.passed is False
        assert excinfo.value.report.errors


# ---------------------------------------------------------------------------
# DOCX export
# ---------------------------------------------------------------------------

class TestDocumentExporter:
    def test_headings_and_paragraphs_render(self) -> None:
        document = markdown_to_document("# Title\n\n## Section\n\nBody text.")
        texts = [p.text for p in document.paragraphs]
        assert "Title" in texts and "Section" in texts and "Body text." in texts

    def test_table_renders_with_correct_shape(self) -> None:
        markdown = "| Activity | Owner |\n| --- | --- |\n| Review | QA |\n| Approve | Head |"
        table = markdown_to_document(markdown).tables[0]
        assert len(table.rows) == 3 and len(table.columns) == 2
        assert table.cell(0, 0).text == "Activity"
        assert table.cell(2, 1).text == "Head"

    def test_ragged_table_row_is_padded(self) -> None:
        table = markdown_to_document("| A | B | C |\n| --- | --- | --- |\n| 1 | 2 |").tables[0]
        assert len(table.columns) == 3 and table.cell(1, 2).text == ""

    def test_inline_bold_is_applied(self) -> None:
        runs = markdown_to_document("This is **critical** guidance.").paragraphs[-1].runs
        assert any(run.bold and run.text == "critical" for run in runs)

    def test_lists_and_code_do_not_raise(self) -> None:
        markdown = "- bullet one\n\n1. step one\n\n```\ncode line\n```\n\n---\n\n> quote"
        document = markdown_to_document(markdown)
        assert any("bullet one" in p.text for p in document.paragraphs)
        assert any("code line" in p.text for p in document.paragraphs)

    def test_full_sop_exports_without_error(self, payload: ProjectPayload, tmp_path: Path) -> None:
        _id, markdown, _meta, _r = MasterOrchestrator(client=FakeOllamaClient()).generate(payload)
        path = export_markdown_to_docx(markdown, "SOP-FULL-TEST", output_dir=tmp_path)
        assert path.stat().st_size > 10000

    def test_export_writes_readable_file(self, tmp_path: Path) -> None:
        path = export_markdown_to_docx(
            "# SOP\n\n## 1. Document Control\n\nContent.",
            "SOP-TEST-20260101-ABCD1234",
            output_dir=tmp_path,
        )
        assert path.exists() and path.suffix == ".docx" and path.stat().st_size > 0

    def test_resolve_finds_and_misses_correctly(self, tmp_path: Path) -> None:
        export_markdown_to_docx("# X", "SOP-FIND-ME", output_dir=tmp_path)
        assert resolve_docx_path("SOP-FIND-ME", output_dir=tmp_path) is not None
        assert resolve_docx_path("SOP-DOES-NOT-EXIST", output_dir=tmp_path) is None

    def test_path_traversal_is_neutralised(self, tmp_path: Path) -> None:
        path = export_markdown_to_docx("# X", "../../escape", output_dir=tmp_path)
        assert path.parent == tmp_path and ".." not in path.name


# ---------------------------------------------------------------------------
# API and web application
# ---------------------------------------------------------------------------

class TestAPI:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path: Path) -> TestClient:
        """TestClient with local inference and artifact storage stubbed out."""
        import app.main as main_module
        from app.config import settings

        monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(main_module, "get_client", lambda: FakeOllamaClient())
        monkeypatch.setattr(
            main_module,
            "MasterOrchestrator",
            lambda: MasterOrchestrator(client=FakeOllamaClient()),
        )
        return TestClient(app)

    def test_info_lists_the_pipeline(self, client: TestClient) -> None:
        body = client.get("/api/v1/info").json()
        assert body["offline"] is True
        assert len(body["pipeline"]) == 4

    def test_health_reports_ready(self, client: TestClient) -> None:
        assert client.get("/api/v1/health").json()["status"] == "ready"

    def test_web_app_is_served_at_root(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "SOP Builder" in response.text

    def test_web_app_has_no_external_resources(self, client: TestClient) -> None:
        """The page must work air-gapped: no CDN, fonts, or remote scripts."""
        html = client.get("/").text
        for marker in ("http://cdn", "https://cdn", "googleapis", "unpkg", "jsdelivr"):
            assert marker not in html

    def test_generate_returns_markdown_url_and_validation(self, client: TestClient) -> None:
        body = client.post("/api/v1/sop/generate", json=SAMPLE_PAYLOAD).json()
        assert set(body) == {"document_id", "markdown_content", "docx_download_url", "validation"}
        assert body["document_id"] in body["docx_download_url"]
        assert body["validation"]["passed"] is True
        assert body["validation"]["phases_found"] == [1, 2, 3, 4]

    def test_generate_then_download_roundtrip(self, client: TestClient) -> None:
        document_id = client.post("/api/v1/sop/generate", json=SAMPLE_PAYLOAD).json()["document_id"]
        download = client.get(f"/api/v1/sop/download/{document_id}")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert download.content[:2] == b"PK"

    def test_download_unknown_id_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/sop/download/SOP-NOPE-00000000-DEADBEEF").status_code == 404

    def test_invalid_payload_returns_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/sop/generate", json={"project_name": "X"}).status_code == 422

    def test_daemon_outage_returns_503_with_hint(self, monkeypatch, tmp_path: Path) -> None:
        import app.main as main_module
        from app.config import settings

        monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(
            main_module,
            "MasterOrchestrator",
            lambda: MasterOrchestrator(client=FakeOllamaClient(fail=True)),
        )
        with TestClient(app, raise_server_exceptions=False) as offline:
            response = offline.post("/api/v1/sop/generate", json=SAMPLE_PAYLOAD)
        assert response.status_code == 503
        assert "hint" in response.json()

    def test_validation_failure_returns_422_with_report(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        import app.main as main_module
        from app.config import settings

        monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

        def broken_orchestrator():
            orchestrator = MasterOrchestrator(client=FakeOllamaClient())
            orchestrator._assemble = lambda **_kwargs: "# Broken\n\nNothing."
            return orchestrator

        monkeypatch.setattr(main_module, "MasterOrchestrator", broken_orchestrator)
        with TestClient(app, raise_server_exceptions=False) as strict:
            response = strict.post("/api/v1/sop/generate", json=SAMPLE_PAYLOAD)
        assert response.status_code == 422
        body = response.json()
        assert body["validation"]["passed"] is False
        assert body["validation"]["issues"]
