"""Pass 2 - Technical Stack Matching Agent.

Every tool in the input array must receive its own sub-block in section 4; no
tool may be omitted. The agent also produces the four mandated failure-mode
categories. Output is checked by `app.services.validators` and re-prompted with
explicit repair hints when it falls short.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from app.schemas import ProjectPayload
from app.services.ollama_client import OllamaClient, get_client

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """\
You are a Senior Government Systems Engineer authoring section 4 (Tooling
Architecture and Technical Prerequisites) of an official Standard Operating
Procedure, for a public-sector IT directorate running an air-gapped estate.

You never ask clarifying questions and never write "TBD", "N/A", or
"[insert value]". Where a detail is absent, infer the most standard, defensible
government-practice value and state it as an explicit assumption.

Produce ONLY GitHub-flavoured Markdown starting at heading level 3 (###). No
document title, no preamble, no closing commentary.

Emit exactly these subsections, in order:

### Operating Environment
Host class, OS baseline, runtime versions, network zone (air-gapped / LAN /
DMZ), and storage paths. Give real absolute paths, e.g. `/opt/sop/data`.

### Tool Sub-Blocks
CRITICAL REQUIREMENT: emit one `#### <Tool Name>` sub-block for EVERY tool in
the supplied list. Omitting any tool makes the document invalid and it will be
rejected. Each sub-block must contain:
- **Version:** exact minimum version.
- **Role in this project:** one sentence tied to this specific project.
- **Provisioning:** the exact install/provisioning command, in a fenced code block.
- **Configuration:** the config file path and the key settings that matter.
- **Verification:** a fenced code block with the command that proves the tool
  works, plus the expected output.
- **Access required:** the account, role, or privilege needed.

### Data Flow Architecture
The end-to-end path as a numbered list of hops. For each hop: source,
transformation, destination, and on-the-wire format. Name real paths, table
names, and dataset names.

### Execution Cadence
What runs daily, weekly, monthly, or on demand; the runtime window; and the
dependency order between routines.

### Interfaces and Integration Points
Internal endpoints, database connection strings, file drop directories, and
export targets. Use internal-only addresses such as `http://10.20.0.14:5432`.
Never reference an external public API.

### Failure Modes and Mitigations
A Markdown table: | # | Failure Mode | Likely Trigger | Detection Signal |
Immediate Mitigation | Escalation Owner |.
It MUST contain at least six rows, and MUST cover all four of these categories
explicitly:
1. Data corruption or schema validation failure.
2. Database or network connection timeout or deadlock.
3. Resource exhaustion - disk space, RAM, or container crash.
4. Downstream synchronisation or report/dashboard refresh failure.
Detection signals must be concrete: an exact error string, exit code, or log line.

### Technical Assumptions
Bulleted list of every assumption made in place of missing input.
"""

_USER_TEMPLATE = """\
Project Name: {project_name}
Owning Department: {department}
Security Classification: {classification}

Project Description:
{description}

Declared Tool Stack ({tool_count} tools - EVERY ONE needs its own #### sub-block):
{tools}

Document the complete technical execution context for this project.
"""

_REPAIR_TEMPLATE = """\
Your previous section 4 draft FAILED automated structural validation.

Project: {project_name} ({department})
Declared Tool Stack ({tool_count}): {tool_list}

DEFECTS FOUND:
{defects}

REQUIRED CORRECTIONS:
{hints}

Rewrite section 4 IN FULL, correcting every defect above and preserving the
parts that were already correct. Emit only the Markdown for section 4, starting
at '### Operating Environment'.

Your previous draft, for reference:
---
{previous}
---
"""

_FALLBACK_TOOLS = ["Python", "SQL Database", "Spreadsheet Reporting"]

# Deterministic per-tool detail used when the model omits a tool entirely.
_TOOL_PROFILES = {
    "python": {
        "version": "3.11",
        "role": "Executes the extraction, validation, and transformation scripts.",
        "install": "sudo apt-get install -y python3.11 python3.11-venv\npython3.11 -m venv /opt/sop/venv\n/opt/sop/venv/bin/pip install -r /opt/sop/requirements.txt",
        "config": "`/opt/sop/config/settings.ini` - defines input paths, batch size, and log level.",
        "verify": "/opt/sop/venv/bin/python --version\n# Expected: Python 3.11.x",
        "access": "Standard service account `svc_sop` with read/write on `/opt/sop`.",
    },
    "postgresql": {
        "version": "15",
        "role": "System of record for validated project data and staging tables.",
        "install": "sudo apt-get install -y postgresql-15 postgresql-client-15\nsudo systemctl enable --now postgresql",
        "config": "`/etc/postgresql/15/main/postgresql.conf` - `max_connections`, `work_mem`, and WAL archiving.",
        "verify": "psql -h 10.20.0.14 -U svc_sop -d sop_db -c \"SELECT version();\"\n# Expected: PostgreSQL 15.x on x86_64-pc-linux-gnu",
        "access": "Database role `svc_sop` with SELECT/INSERT/UPDATE on the project schema.",
    },
    "postgres": {"alias": "postgresql"},
    "sql": {
        "version": "ANSI SQL / vendor current",
        "role": "Query language for staging, reconciliation, and reporting extracts.",
        "install": "# Provided by the database client package\nsudo apt-get install -y postgresql-client-15",
        "config": "`~/.psqlrc` - sets `ON_ERROR_STOP=1` so failed batches abort rather than half-apply.",
        "verify": "psql -d sop_db -c \"SELECT 1;\"\n# Expected: a single row returning 1",
        "access": "Read/write on the project schema; DDL restricted to the Technical Lead.",
    },
    "powerbi": {
        "version": "Power BI Desktop (current LTS) with on-premises Data Gateway",
        "role": "Publishes the compliance dashboard consumed by departmental officers.",
        "install": "# Install the on-premises data gateway on the reporting host\n# Gateway installer is distributed via the internal software repository\nStart-Process -Wait -FilePath .\\GatewayInstall.exe -ArgumentList '/quiet'",
        "config": "Gateway data source points at `10.20.0.14:5432`, database `sop_db`, using the stored `svc_sop` credential.",
        "verify": "Get-Service -Name PBIEgwService\n# Expected: Status = Running",
        "access": "Workspace Contributor on the departmental Power BI workspace.",
    },
    "docker": {
        "version": "24.0",
        "role": "Runs the processing services in reproducible, isolated containers.",
        "install": "sudo apt-get install -y docker.io docker-compose-plugin\nsudo systemctl enable --now docker",
        "config": "`/opt/sop/docker-compose.yml` - service definitions, volume mounts, and restart policy.",
        "verify": "docker compose -f /opt/sop/docker-compose.yml ps\n# Expected: every service shows State = running (healthy)",
        "access": "Membership of the `docker` group, granted to the operations account only.",
    },
}


def _active_tools(payload: ProjectPayload) -> List[str]:
    tools = [tool.strip() for tool in payload.tools if tool and tool.strip()]
    return tools or list(_FALLBACK_TOOLS)


def _format_tools(payload: ProjectPayload) -> str:
    """List the tools, attaching any officer-supplied description.

    A description supplied for a tool the model may not know is authoritative:
    it was either drafted and accepted, or written, by the reviewing officer.
    """
    lines = []
    for tool in _active_tools(payload):
        detail = payload.detail_for(tool)
        if detail and detail.description.strip():
            version = (
                f" (typical version: {detail.typical_version})"
                if detail.typical_version
                else ""
            )
            lines.append(
                f"- {tool}{version}\n"
                f"    OFFICER-SUPPLIED DESCRIPTION (authoritative, use this): "
                f"{detail.description.strip()}"
            )
        else:
            lines.append(f"- {tool}")
    return "\n".join(lines)


def build_prompt(payload: ProjectPayload) -> str:
    """Render the user-side prompt for the technical pass."""
    tools = _active_tools(payload)
    return _USER_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        classification=payload.security_classification or "RESTRICTED / INTERNAL",
        description=payload.description.strip(),
        tools=_format_tools(payload),
        tool_count=len(tools),
    )


def build_repair_prompt(payload: ProjectPayload, previous: str, defects: str, hints: str) -> str:
    """Render the retry prompt naming every validation defect."""
    tools = _active_tools(payload)
    return _REPAIR_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        tool_count=len(tools),
        tool_list=", ".join(tools),
        defects=defects,
        hints=hints,
        previous=previous[:6000],
    )


class TechAgent:
    """Pass 2: technical stack matching."""

    name = "TechnicalStackAgent"
    pass_number = 2

    def __init__(self, client: Optional[OllamaClient] = None) -> None:
        self.client = client or get_client()

    def run(self, payload: ProjectPayload) -> str:
        """First-attempt generation of section 4."""
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
def tool_block(tool: str, detail: Optional[Any] = None) -> str:
    """A complete `#### <Tool>` sub-block, used to fill a tool the model dropped.

    `detail` is an optional `ToolDetail` carrying an officer-supplied
    description, which takes precedence over the built-in profile. Falls back to
    a generic-but-concrete profile for tools not in the known set, so the block
    still carries a version, a command, and a verification step.
    """
    key = tool.strip().lower().replace(" ", "").replace("-", "")
    profile = _TOOL_PROFILES.get(key, {})
    if "alias" in profile:
        profile = _TOOL_PROFILES[str(profile["alias"])]

    supplied_description = str(getattr(detail, "description", "") or "").strip()
    supplied_version = str(getattr(detail, "typical_version", "") or "").strip()

    version = supplied_version or profile.get(
        "version", "Latest version approved by the department"
    )
    role = supplied_description or profile.get(
        "role", f"Supports project execution as a {tool} component."
    )
    install = profile.get(
        "install",
        f"# Provision {tool} from the approved internal software repository\n"
        f"sudo apt-get install -y {key or 'package-name'}",
    )
    config = profile.get(
        "config", f"`/opt/sop/config/{key or 'tool'}.conf` - environment-specific settings."
    )
    verify = profile.get(
        "verify", f"{key or tool} --version\n# Expected: the approved version string above"
    )
    access = profile.get("access", "Least-privilege service account approved by the IT Cell.")

    return (
        f"#### {tool}\n\n"
        f"- **Version:** {version}\n"
        f"- **Role in this project:** {role}\n"
        f"- **Access required:** {access}\n"
        f"- **Configuration:** {config}\n\n"
        f"**Provisioning**\n\n```bash\n{install}\n```\n\n"
        f"**Verification**\n\n```bash\n{verify}\n```\n"
    )


def failure_mode_table() -> str:
    """The mandated four-category failure-mode table."""
    return """| # | Failure Mode | Likely Trigger | Detection Signal | Immediate Mitigation | Escalation Owner |
| --- | --- | --- | --- | --- | --- |
| 1 | Data corruption / schema validation failure | Upstream schema change or truncated transfer | `ValidationError: column count mismatch` in `/opt/sop/logs/ingest.log`; checksum mismatch | Quarantine the batch to `/opt/sop/quarantine/`, halt the load, re-request the source extract | Technical Lead |
| 2 | Database connection timeout or deadlock | Network partition, or concurrent writes to the same rows | `psycopg2.OperationalError: timeout expired`; `deadlock detected` in the PostgreSQL log | Retry with backoff (3 attempts); if the deadlock persists, serialise the batch and re-run | Database Administrator |
| 3 | Resource exhaustion - disk space | Batch growth beyond the allocated volume | `No space left on device`; `df -h` shows the data volume above 90% | Archive processed batches to cold storage, then re-run the failed step | Infrastructure Team |
| 4 | Resource exhaustion - RAM / container crash | Oversized batch or a memory leak in the worker | Container `State = Exited (137)`; `dmesg` shows an OOM kill | Reduce `BATCH_SIZE` in `/opt/sop/config/settings.ini`, restart the container, re-run | Infrastructure Team |
| 5 | Downstream sync / dashboard refresh failure | Expired gateway credential or an unavailable dataset | Refresh history shows `DMTS_MonikerWithUnboundDataSource`; stale "last refreshed" timestamp | Re-authenticate the gateway data source, trigger a manual refresh, notify report consumers | Reporting Officer |
| 6 | Scheduled job missed | Scheduler outage or an unplanned host restart | No completion record for the window in `/opt/sop/logs/scheduler.log` | Trigger the run manually, record the deviation, verify no duplicate load occurred | Technical Lead |"""


def fallback_section(payload: ProjectPayload) -> str:
    """Deterministic section 4 that satisfies every structural gate.

    Used when the model returns nothing usable. It is complete and valid, and
    marks itself as requiring officer review before approval.
    """
    tools = _active_tools(payload)
    blocks = "\n".join(tool_block(tool, payload.detail_for(tool)) for tool in tools)
    department = payload.department

    return f"""### Operating Environment

Processing runs on hardened departmental servers inside the internal LAN of the
{department}. The application host, the database host, and the reporting host
sit in the same restricted network zone. Working data is held under
`/opt/sop/data`, logs under `/opt/sop/logs`, and archived batches under
`/opt/sop/archive`. No component of this workflow transmits data outside the
departmental network boundary.

### Tool Sub-Blocks

{blocks}

### Data Flow Architecture

1. **Source extract → landing zone.** The departmental system of record deposits
   a dated extract into `/opt/sop/data/incoming/YYYY-MM-DD/` as UTF-8 CSV.
2. **Landing zone → validation.** The ingestion script reads each file, checks
   the header against the agreed schema, and writes rejects to
   `/opt/sop/quarantine/`.
3. **Validation → staging tables.** Accepted rows load into `stg_records` in
   `sop_db` with the batch identifier attached.
4. **Staging → production tables.** A reconciled merge promotes staged rows into
   `fact_records`, resolving primary-key collisions on the natural key.
5. **Production → reporting dataset.** The reporting dataset refreshes from
   `fact_records` and publishes to the departmental dashboard.

### Execution Cadence

| Routine | Cadence | Window | Depends On |
| --- | --- | --- | --- |
| Environment health check | Daily | 07:30-07:45 | None |
| Ingestion and validation | Daily | 07:45-08:30 | Health check passed |
| Database load and indexing | Daily | 08:30-09:15 | Ingestion complete |
| Dashboard refresh | Daily | 09:15-09:45 | Load complete |
| Reconciliation review | Weekly (Friday) | 15:00-16:00 | Five daily cycles complete |

### Interfaces and Integration Points

| Interface | Address | Protocol | Purpose |
| --- | --- | --- | --- |
| Project database | `10.20.0.14:5432` (`sop_db`) | PostgreSQL wire protocol | Staging and production storage |
| File drop | `/opt/sop/data/incoming/` | Local filesystem (LAN mount) | Source extract delivery |
| Processing service | `http://10.20.0.21:8080/health` | HTTP (internal only) | Service health probe |
| Reporting gateway | `10.20.0.30` | On-premises data gateway | Dashboard dataset refresh |

All integration points are internal. External network egress is not permitted
under this procedure.

### Failure Modes and Mitigations

{failure_mode_table()}

### Technical Assumptions

- Automated technical inference was unavailable at generation time; this section
  states baseline departmental defaults and **must be reviewed and confirmed by
  the Technical Lead before the SOP is approved.**
- Hosts, IP addresses, and paths shown are the departmental standard pattern and
  must be replaced with the actual values recorded in the asset register.
- All tooling is provisioned from the approved internal software repository.
"""


def repair_missing_tools(
    section: str, missing: Sequence[str], payload: Optional[ProjectPayload] = None
) -> str:
    """Deterministically append sub-blocks for tools the model omitted.

    Inserted under the `### Tool Sub-Blocks` heading when present, so the
    document keeps its intended section order.
    """
    if not missing:
        return section
    additions = "\n".join(
        tool_block(tool, payload.detail_for(tool) if payload else None) for tool in missing
    )

    # Insert before whichever subsection follows the tool blocks, so the new
    # blocks land inside "Tool Sub-Blocks" rather than at the end of the
    # section, where the failure-table repair would truncate them away.
    for marker in ("### Data Flow Architecture", "### Failure Modes and Mitigations"):
        if marker in section:
            head, _, tail = section.partition(marker)
            return f"{head.rstrip()}\n\n{additions}\n\n{marker}{tail}"
    if "### Tool Sub-Blocks" in section:
        return f"{section.rstrip()}\n\n{additions}\n"
    return f"{section.rstrip()}\n\n### Tool Sub-Blocks\n\n{additions}\n"


def repair_failure_modes(section: str) -> str:
    """Replace the failure-mode table with the compliant version.

    Only the table is swapped: every subsection after it is preserved verbatim,
    whatever it is called, so a repair never silently deletes content the model
    (or an earlier repair) produced.
    """
    heading = "### Failure Modes and Mitigations"
    if heading not in section:
        return f"{section.rstrip()}\n\n{heading}\n\n{failure_mode_table()}\n"

    head, _, tail = section.partition(heading)
    tail_lines = tail.split("\n")
    remainder = ""
    for index, line in enumerate(tail_lines):
        if line.startswith("### "):
            remainder = "\n".join(tail_lines[index:])
            break
    return f"{head}{heading}\n\n{failure_mode_table()}\n\n{remainder}".rstrip() + "\n"
