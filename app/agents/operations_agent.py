"""Pass 3 - Deep Operational Execution Agent.

Generates section 5 (Operating Execution Phases) and section 6 (Quality
Assurance), with mandatory tactical depth. High-level directives such as
"Verify access" are rejected by the validation gate; every step must carry an
exact command, query, or endpoint, the data format and path it acts on, the
verification action, and its failure fallback.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from app.schemas import ProjectPayload
from app.services.ollama_client import OllamaClient, get_client

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """\
You are the Chief Procedure Officer of a government department, writing the
executable core of an official Standard Operating Procedure.

You receive a project brief, a technical analysis, and a governance analysis.
Write sections 5 and 6 so that an operator who has never seen this system can
execute it correctly from the page alone.

OUTPUT FORMAT
Write ONLY GitHub-flavoured Markdown. Emit exactly two top-level sections, in
this order and with these exact headings:
"## 5. Operating Execution Phases"
"## 6. Quality Assurance and Controls"
Do not repeat the RACI matrix or the tooling table; reference them instead.
Use formal, impersonal, instructional register ("The Data Operations Officer
shall ..."). Never first or second person. Never "TBD" or "[insert value]".

SECTION 5 - MANDATORY PHASES
Emit ALL FOUR of these phases, in this order, each as a "### Phase N - <Name>"
heading. A draft ending early at Phase 1 or 2 is rejected automatically:

Phase 1 - Environment Setup & Health Checks
  CLI commands that verify container state, database connectivity, and
  credential validity before any data is touched.
Phase 2 - Ingestion & Data Transformation
  The ingestion script invocation, the source directory rules, file naming and
  encoding, and the transformation commands.
Phase 3 - Database Load & Indexing
  SQL for staging, primary-key collision handling, index maintenance, and
  explicit commit/rollback logic.
Phase 4 - Reporting, Dashboards & Export
  Dataset/gateway refresh steps, export generation, log publication, and the
  sign-off action.

DEPTH RULES - every phase MUST contain all of the following:
1. **Objective**, **Preconditions**, and **Responsible Role** lines.
2. A numbered list of at least five steps. EVERY step must contain an exact CLI
   command, SQL query, or API endpoint inside a fenced code block. A step such
   as "Verify access" with no command is INVALID.
3. Explicit input data formats and absolute directory or schema paths
   (for example `/opt/sop/data/incoming/YYYY-MM-DD/*.csv`, table `stg_records`).
4. A verification action after each significant step: the exact expected output,
   row count, exit code, or on-screen indicator the operator should see.
5. An **Error Handling** block naming at least two concrete failure signals
   (exact error text or exit code), the fallback command for each, and the role
   escalated to.
6. An **Exit Criteria** line stating the verifiable condition for completion.

SECTION 6 must contain:
- "### Quality Control Checkpoints" - a table:
  | Checkpoint | Frequency | Method | Acceptance Threshold | Recorded By |.
- "### Audit Trail Requirements" - what is logged, the exact log path, and the
  retention period.
- "### Deviation and Non-Conformance Handling" - how a deviation is recorded,
  reviewed, and closed.
"""

_USER_TEMPLATE = """\
Project Name: {project_name}
Owning Department: {department}
Security Classification: {classification}
Tool Stack: {tools}

Project Description:
{description}

--- TECHNICAL ANALYSIS (section 4, from the Systems Engineer) ---
{tech_analysis}

--- GOVERNANCE ANALYSIS (section 3, from the Governance Officer) ---
{governance_analysis}

Write sections 5 and 6. All four mandatory phases are required, each with exact
commands, paths, verification steps, and error handling.
"""

_REPAIR_TEMPLATE = """\
Your previous draft of sections 5 and 6 FAILED automated depth validation.

Project: {project_name} ({department})
Tool Stack: {tools}

DEFECTS FOUND:
{defects}

REQUIRED CORRECTIONS:
{hints}

Rewrite sections 5 and 6 IN FULL. All four mandatory phases must be present
(Phase 1 Environment Setup & Health Checks, Phase 2 Ingestion & Data
Transformation, Phase 3 Database Load & Indexing, Phase 4 Reporting, Dashboards
& Export). Every phase needs commands in fenced code blocks and an explicit
Error Handling block. Start at '## 5. Operating Execution Phases'.

Your previous draft, for reference:
---
{previous}
---
"""


def _tools(payload: ProjectPayload) -> str:
    active = [tool.strip() for tool in payload.tools if tool and tool.strip()]
    return ", ".join(active) or "departmental standard tooling"


def build_prompt(payload: ProjectPayload, tech_analysis: str, governance_analysis: str) -> str:
    """Render the user-side prompt for the operational pass."""
    return _USER_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        classification=payload.security_classification or "RESTRICTED / INTERNAL",
        description=payload.description.strip(),
        tools=_tools(payload),
        tech_analysis=tech_analysis[:8000],
        governance_analysis=governance_analysis[:6000],
    )


def build_repair_prompt(payload: ProjectPayload, previous: str, defects: str, hints: str) -> str:
    """Render the retry prompt naming every validation defect."""
    return _REPAIR_TEMPLATE.format(
        project_name=payload.project_name,
        department=payload.department,
        tools=_tools(payload),
        defects=defects,
        hints=hints,
        previous=previous[:7000],
    )


class OperationsAgent:
    """Pass 3: deep operational execution."""

    name = "DeepExecutionAgent"
    pass_number = 3

    def __init__(self, client: Optional[OllamaClient] = None) -> None:
        self.client = client or get_client()

    def run(self, payload: ProjectPayload, tech_analysis: str, governance_analysis: str) -> str:
        """First-attempt generation of sections 5 and 6."""
        result = self.client.generate(
            system_prompt=SYSTEM_INSTRUCTION,
            user_prompt=build_prompt(payload, tech_analysis, governance_analysis),
        )
        return result if result.strip() else fallback_section(payload)

    def repair(self, payload: ProjectPayload, previous: str, defects: str, hints: str) -> str:
        """Retry generation with the validator's defects fed back in."""
        result = self.client.generate(
            system_prompt=SYSTEM_INSTRUCTION,
            user_prompt=build_repair_prompt(payload, previous, defects, hints),
            think=False,
        )
        return result if result.strip() else previous


# ---------------------------------------------------------------------------
# Deterministic phase templates
# ---------------------------------------------------------------------------
# Written without f-strings: the bodies contain SQL and JSON braces.

_PHASE_1 = """### Phase 1 - Environment Setup & Health Checks

**Objective:** Prove that every container, database connection, and credential
is healthy before any project data is touched.
**Preconditions:** The Initiation Gate in section 3 has been cleared and
recorded.
**Responsible Role:** Technical Lead.

1. Confirm every processing container is running and healthy.

```bash
docker compose -f /opt/sop/docker-compose.yml ps
```

   Verification: every service reports `State = running (healthy)`. A service in
   `Exited` or `Restarting` state blocks the cycle.

2. Confirm the database is reachable and accepting connections.

```bash
psql -h 10.20.0.14 -U svc_sop -d sop_db -c "SELECT now(), current_user;"
```

   Verification: one row returns, showing the current timestamp and `svc_sop`.

3. Confirm the service credential has not expired.

```bash
psql -h 10.20.0.14 -U svc_sop -d sop_db -tAc \\
  "SELECT rolvaliduntil FROM pg_roles WHERE rolname = 'svc_sop';"
```

   Verification: the returned date is at least 14 days in the future. If it is
   nearer than that, raise an access-renewal request before proceeding.

4. Confirm sufficient disk headroom on the data volume.

```bash
df -h /opt/sop/data
```

   Verification: `Use%` is below 85%. At or above 85%, archive processed batches
   before continuing.

5. Confirm the processing service answers its health probe.

```bash
curl -s -o /dev/null -w "%{http_code}\\n" http://10.20.0.21:8080/health
```

   Verification: the response is `200`.

6. Record the health check outcome in the daily operations log.

```bash
echo "$(date -Iseconds) health-check PASS $(whoami)" >> /opt/sop/logs/operations.log
```

**Error Handling**

- If `docker compose ps` shows `Exited (137)`, the container was killed for
  exceeding memory. Restart it with
  `docker compose -f /opt/sop/docker-compose.yml up -d --force-recreate <service>`,
  then re-run step 1. If it exits again, escalate to the Infrastructure Team.
- If `psql` returns `could not connect to server: Connection refused`, confirm
  the database host is up (`ping -c 3 10.20.0.14`). If the host answers but the
  port does not, escalate to the Database Administrator. Do not proceed to
  Phase 2 with a failed connection.
- If the health probe returns anything other than `200`, capture the service log
  (`docker compose logs --tail 200 processor > /opt/sop/logs/health-fail.log`)
  and escalate to the Technical Lead.

**Exit Criteria:** All six checks pass and the PASS line is written to
`/opt/sop/logs/operations.log`.
"""

_PHASE_2 = """### Phase 2 - Ingestion & Data Transformation

**Objective:** Bring the day's source extract into the landing zone, validate
its structure, and transform it into the staging format.
**Preconditions:** Phase 1 exit criteria met.
**Responsible Role:** Data Operations Officer.

**Input data format:** UTF-8 CSV, comma-delimited, quoted text fields, header
row required. Files arrive as
`/opt/sop/data/incoming/YYYY-MM-DD/<district_code>_<batch>.csv`. Any file not
matching this pattern is not processed.

1. Confirm the expected files have arrived for the processing date.

```bash
ls -l /opt/sop/data/incoming/$(date +%F)/*.csv | wc -l
```

   Verification: the count matches the expected district count for the cycle. A
   shortfall is recorded as a deviation before proceeding.

2. Verify file integrity against the supplied checksum manifest.

```bash
cd /opt/sop/data/incoming/$(date +%F) && sha256sum -c manifest.sha256
```

   Verification: every line reports `OK`. Any `FAILED` line means a corrupt
   transfer; move that file to `/opt/sop/quarantine/` and re-request it.

3. Validate the header and column types against the agreed schema.

```bash
/opt/sop/venv/bin/python -m sop.validate \\
  --input /opt/sop/data/incoming/$(date +%F) \\
  --schema /opt/sop/config/schema.json \\
  --reject-dir /opt/sop/quarantine
```

   Verification: the command exits `0` and prints
   `validated: <n> rows, rejected: <m> rows`.

4. Run the transformation into the staging format.

```bash
/opt/sop/venv/bin/python -m sop.transform \\
  --input /opt/sop/data/incoming/$(date +%F) \\
  --output /opt/sop/data/staged/$(date +%F) \\
  --batch-id "$(date +%Y%m%d)-01"
```

   Verification: `/opt/sop/data/staged/$(date +%F)/` contains one `.parquet`
   file per input file, and the run prints `transform complete`.

5. Reconcile transformed row counts against the source.

```bash
/opt/sop/venv/bin/python -m sop.reconcile \\
  --source /opt/sop/data/incoming/$(date +%F) \\
  --staged /opt/sop/data/staged/$(date +%F)
```

   Verification: the variance line reads `variance: 0` once rejects are
   accounted for. Any unexplained variance halts the cycle.

6. Record the ingestion outcome, including volumes and reject counts.

```bash
echo "$(date -Iseconds) ingest batch=$(date +%Y%m%d)-01 rows=<n> rejects=<m>" \\
  >> /opt/sop/logs/ingest.log
```

**Error Handling**

- `ValidationError: column count mismatch` means the upstream schema changed.
  Stop the cycle, retain the file in `/opt/sop/quarantine/`, and notify the data
  custodian; do not edit the source file by hand.
- Exit code `2` from `sop.transform` indicates a malformed record. Inspect
  `/opt/sop/logs/ingest.log`, re-run with `--skip-invalid` only when the
  Technical Lead has authorised it in writing, and record the authorisation.
- If reconciliation reports a non-zero variance, do not proceed to Phase 3. The
  Data Operations Officer investigates and escalates to the Technical Lead if
  unresolved within one working day.

**Exit Criteria:** Staged files exist for every accepted input, reconciliation
variance is zero, and the ingestion line is written to
`/opt/sop/logs/ingest.log`.
"""

_PHASE_3 = """### Phase 3 - Database Load & Indexing

**Objective:** Load staged data into the database, resolve primary-key
collisions deterministically, refresh indexes, and commit the batch atomically.
**Preconditions:** Phase 2 exit criteria met.
**Responsible Role:** Database Administrator, supported by the Technical Lead.

**Target schema:** `sop_db` - staging table `stg_records`, production table
`fact_records`, natural key `(district_code, record_number)`.

1. Truncate the staging table so no prior batch remains.

```sql
BEGIN;
TRUNCATE TABLE stg_records;
```

   Verification: `SELECT count(*) FROM stg_records;` returns `0`.

2. Load the staged files into the staging table.

```bash
/opt/sop/venv/bin/python -m sop.load \\
  --input /opt/sop/data/staged/$(date +%F) \\
  --table stg_records --batch-id "$(date +%Y%m%d)-01"
```

   Verification: the printed row count equals the reconciled count from Phase 2.

3. Detect primary-key collisions BEFORE promoting any row.

```sql
SELECT district_code, record_number, count(*)
FROM stg_records
GROUP BY district_code, record_number
HAVING count(*) > 1;
```

   Verification: zero rows returned. If rows are returned, the duplicates are
   exported to `/opt/sop/quarantine/duplicates_$(date +%F).csv` and excluded
   from the load; the batch does not proceed until the district office confirms
   the correct record.

4. Promote staged rows into production, resolving collisions by keeping the most
   recently amended record.

```sql
INSERT INTO fact_records AS f (district_code, record_number, owner_name, amended_at, batch_id)
SELECT district_code, record_number, owner_name, amended_at, batch_id
FROM stg_records
ON CONFLICT (district_code, record_number) DO UPDATE
   SET owner_name = EXCLUDED.owner_name,
       amended_at = EXCLUDED.amended_at,
       batch_id   = EXCLUDED.batch_id
 WHERE EXCLUDED.amended_at > f.amended_at;
```

   Verification: the reported `INSERT 0 <n>` count is consistent with the staged
   volume.

5. Refresh indexes and statistics so query plans stay valid.

```sql
REINDEX TABLE CONCURRENTLY fact_records;
ANALYZE fact_records;
```

   Verification: both statements complete without error.

6. Commit the batch, or roll it back if any check above failed.

```sql
COMMIT;
-- If any verification failed, run instead:
-- ROLLBACK;
```

   Verification: `SELECT count(*) FROM fact_records WHERE batch_id = '<batch>';`
   matches the promoted count.

7. Record the load outcome.

```bash
echo "$(date -Iseconds) load batch=$(date +%Y%m%d)-01 committed rows=<n>" \\
  >> /opt/sop/logs/load.log
```

**Error Handling**

- `deadlock detected` means concurrent writes collided. Roll back, wait 60
  seconds, and re-run the load once. If the deadlock recurs, serialise the batch
  (`SET LOCAL max_parallel_workers = 0;`) and escalate to the Database
  Administrator.
- `duplicate key value violates unique constraint` means step 3 was skipped or
  incomplete. Roll back immediately; never resolve a collision by deleting
  production rows.
- `No space left on device` during REINDEX means the tablespace is full. Roll
  back, free space, and escalate to the Infrastructure Team before retrying.
- An interrupted session leaves the transaction open. Confirm with
  `SELECT state FROM pg_stat_activity WHERE usename = 'svc_sop';` and roll back
  any `idle in transaction` session before restarting the phase.

**Exit Criteria:** The batch is committed, the production row count reconciles
against Phase 2, and the load line is written to `/opt/sop/logs/load.log`.
"""

_PHASE_4 = """### Phase 4 - Reporting, Dashboards & Export

**Objective:** Refresh the reporting dataset, generate the official export,
publish the run logs, and obtain sign-off.
**Preconditions:** Phase 3 exit criteria met and the batch committed.
**Responsible Role:** Reporting Officer, signed off by the Accountable Officer.

1. Confirm the on-premises data gateway is running before triggering a refresh.

```powershell
Get-Service -Name PBIEgwService
```

   Verification: `Status = Running`. If stopped, start it with
   `Start-Service -Name PBIEgwService` and re-check.

2. Trigger the dataset refresh.

```bash
curl -X POST "http://10.20.0.30/v1.0/myorg/datasets/<dataset-id>/refreshes" \\
  -H "Content-Type: application/json" \\
  -d '{"notifyOption": "MailOnFailure"}'
```

   Verification: the call returns HTTP `202 Accepted`.

3. Confirm the refresh completed rather than merely starting.

```bash
curl -s "http://10.20.0.30/v1.0/myorg/datasets/<dataset-id>/refreshes?$top=1"
```

   Verification: the latest entry shows `"status": "Completed"`. A status of
   `Failed` blocks publication.

4. Verify the dashboard reflects the current batch in the user interface.

   Open the departmental dashboard, confirm the "Last refreshed" caption shows
   today's date, and confirm the record total matches the Phase 3 committed
   count. A mismatch means the dataset is serving cached data; do not publish.

5. Generate the official export for circulation.

```bash
/opt/sop/venv/bin/python -m sop.export \\
  --batch-id "$(date +%Y%m%d)-01" \\
  --format xlsx \\
  --output /opt/sop/exports/compliance_$(date +%F).xlsx
```

   Verification: the file exists and is non-empty
   (`ls -lh /opt/sop/exports/compliance_$(date +%F).xlsx`).

6. Publish the run logs to the audit store.

```bash
cp /opt/sop/logs/{ingest,load,operations}.log \\
   /opt/sop/archive/$(date +%F)/
```

   Verification: all three files are present in the dated archive directory.

7. Record sign-off against the Release Gate defined in section 3.

```bash
echo "$(date -Iseconds) release batch=$(date +%Y%m%d)-01 signed-off-by=<officer>" \\
  >> /opt/sop/logs/operations.log
```

**Error Handling**

- Refresh status `Failed` with `DMTS_MonikerWithUnboundDataSource` means the
  gateway credential is no longer bound. Re-authenticate the data source in the
  gateway configuration, then repeat step 2. If it fails twice, escalate to the
  Reporting Officer and notify report consumers that figures are stale.
- HTTP `429` means the daily refresh limit is exhausted. Do not retry in a loop;
  schedule the refresh for the next available window and record the deviation.
- If the export command exits non-zero, do not circulate a partial file. Delete
  it, re-run once, and escalate to the Technical Lead if it fails again.
- If the dashboard total does not match the committed count, treat it as a
  reconciliation failure: withhold publication and escalate to the Quality
  Assurance Officer the same working day.

**Exit Criteria:** Dataset refresh shows `Completed`, dashboard totals reconcile
with Phase 3, the export exists in `/opt/sop/exports/`, logs are archived, and
the Accountable Officer has countersigned the release note.
"""

_SECTION_6 = """## 6. Quality Assurance and Controls

### Quality Control Checkpoints

| Checkpoint | Frequency | Method | Acceptance Threshold | Recorded By |
| --- | --- | --- | --- | --- |
| Environment health | Daily, before ingestion | Phase 1 command set | All six checks pass | Technical Lead |
| Access and credential validity | Daily | `pg_roles` expiry query | Credential valid for 14+ days | Technical Lead |
| Source file integrity | Per batch | `sha256sum -c manifest.sha256` | 100% `OK` lines | Data Operations Officer |
| Schema validation | Per batch | `sop.validate` exit code | Exit `0`, no critical rejects | Data Operations Officer |
| Ingestion reconciliation | Per batch | Source-to-staged row comparison | Variance = 0 after rejects | Data Operations Officer |
| Primary-key collisions | Per batch | Duplicate-detection query | Zero unresolved duplicates | Database Administrator |
| Load reconciliation | Per batch | Committed count vs staged count | Full agreement | Database Administrator |
| Dashboard refresh | Daily | Refresh history status check | `Completed`, totals reconcile | Reporting Officer |
| Log completeness | Weekly | Archive directory inspection | No gaps in the dated archive | Quality Assurance Officer |

### Audit Trail Requirements

- Every execution records the operator, timestamp, batch identifier, input row
  count, reject count, and outcome.
- Ingestion events are written to `/opt/sop/logs/ingest.log`, load events to
  `/opt/sop/logs/load.log`, and cycle events to `/opt/sop/logs/operations.log`.
- Logs are copied each cycle to `/opt/sop/archive/YYYY-MM-DD/` and are not
  editable by the operating team.
- Quarantined and duplicate records are retained in `/opt/sop/quarantine/` with
  the batch identifier in the filename.
- Approvals, deviations, and delegated decisions are filed in the official
  records system.
- Retention periods are as stated in section 7.1; disposal requires written
  authorisation.

### Deviation and Non-Conformance Handling

Any departure from this procedure is recorded as a deviation at the time it
occurs, stating the step affected, the reason, the compensating action taken,
and the officer who authorised it.

The Responsible officer reviews every deviation within one working day.
Unresolved or repeat deviations escalate through the levels defined in
section 3. Authorisation to re-run with `--skip-invalid`, to publish stale
figures, or to bypass any verification step must be given in writing by the
Accountable Officer and attached to the deviation record.

A deviation is closed only when the Accountable Officer records the corrective
action and confirms, at the next cycle, that it was effective.
"""

PHASE_TEMPLATES = {1: _PHASE_1, 2: _PHASE_2, 3: _PHASE_3, 4: _PHASE_4}


def phase_block(number: int) -> str:
    """The deterministic body for one mandatory phase."""
    return PHASE_TEMPLATES[number]


def fallback_section(_payload: ProjectPayload) -> str:
    """Deterministic sections 5 and 6 that satisfy the depth gate in full."""
    phases = "\n".join(PHASE_TEMPLATES[number] for number in sorted(PHASE_TEMPLATES))
    return f"## 5. Operating Execution Phases\n\n{phases}\n{_SECTION_6}"


def repair_missing_phases(section: str, missing: Sequence[int]) -> str:
    """Splice deterministic phase blocks in for phases the model omitted.

    Inserted before section 6 when present, so phase order and document
    structure are preserved.
    """
    if not missing:
        return section
    additions = "\n".join(phase_block(number) for number in sorted(missing))
    marker = "## 6."
    if marker in section:
        head, _, tail = section.partition(marker)
        return f"{head.rstrip()}\n\n{additions}\n\n{marker}{tail}"
    return f"{section.rstrip()}\n\n{additions}\n"


def ensure_section_six(section: str) -> str:
    """Append the deterministic section 6 when the model omitted it."""
    if "## 6." in section:
        return section
    return f"{section.rstrip()}\n\n{_SECTION_6}"
