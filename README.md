# SOP Builder — Autonomous SOP Generation Engine

Turns a short project brief into a complete, government-standard Standard
Operating Procedure — delivered as a styled Microsoft Word document.

**100% free. Fully offline. No API keys.** Every word is written by a local
[Ollama](https://ollama.com) model on your own machine. Nothing is ever sent to
Google, OpenAI, or anyone else.

---

## For everyday users: just open the app

You do **not** need to use a command line.

| Your computer | What to do |
| --- | --- |
| **Windows** | Double-click **`Start SOP Builder.bat`** |
| **Mac / Linux** | Double-click **`start-sop-builder.sh`** (or run `./start-sop-builder.sh`) |

The launcher sets everything up the first time, then opens the SOP Builder in
your web browser. Fill in the form, press the blue button, and press the green
**Download** button when your document is ready.

The app is designed to be readable and forgiving: large text, plain language,
no jargon, and clear instructions if anything is not set up correctly.

### What the app looks like

```
 ┌──────────────────────────────────────────────────────────┐
 │  SOP Builder                                             │
 ├──────────────────────────────────────────────────────────┤
 │  ✔ The system is ready                                   │
 │                                                          │
 │  Tell us about your project                              │
 │                                                          │
 │  What is the project called?                             │
 │  [ National Land Records Digitisation                 ]  │
 │                                                          │
 │  Which department owns it?                               │
 │  [ Department of Revenue and Land Reforms             ]  │
 │                                                          │
 │  What does the project do?                               │
 │  [ Digitise paper land records across 42 district     ]  │
 │  [ offices and publish a weekly compliance report.    ]  │
 │                                                          │
 │  Who is involved?                                        │
 │  [ A. Krishnan ][ Principal Secretary ][ Revenue ][ ✕ ]  │
 │  [ + Add another person ]                                │
 │                                                          │
 │  Which software or systems are used?                     │
 │  [✔ Python] [✔ PostgreSQL] [✔ PowerBI] [✔ Docker]        │
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │        Create my SOP document                      │  │
 │  └────────────────────────────────────────────────────┘  │
 └──────────────────────────────────────────────────────────┘
```

While it works, you see four plain-language steps ticking off — *"Working out
who is responsible for what"*, *"Writing the step-by-step instructions"*, and so
on — with a running timer, so it never looks frozen.

### One-time setup (IT support does this once per machine)

1. **Install Python 3.11+** — <https://www.python.org/downloads/>
   On Windows, tick **"Add Python to PATH"** during installation.
2. **Install Ollama** — <https://ollama.com/download>
   On Linux/Mac: `curl -fsSL https://ollama.com/install.sh | sh`
3. **Download the model** (about 5 GB, once):

```bash
ollama pull deepseek-r1:8b
```

After that, users only ever double-click the launcher. If something is missing,
the app says so in plain words and shows the exact command to fix it.

---

## Why this exists

Writing an SOP for a government project normally takes several rounds of
interviews with engineers and administrators. This engine collapses that into a
single form: you supply the minimum, and three specialist agents infer the rest.

| Property | Guarantee |
| --- | --- |
| Cost | Zero. Local models only. |
| Network | Air-gapped. The service calls nothing but your local Ollama. |
| Data residency | Input and output never leave the machine. |
| Clarification loops | None. Agents infer and state assumptions instead of asking. |
| Completeness | Enforced in Python, not requested in a prompt. See the validation gate below. |

---

## Architecture: three passes, never one shot

A single LLM call cannot reliably produce a complete SOP — an 8B local model
drifts, truncates, and silently drops tools. So the document is built in three
sequential, individually validated passes:

```
   ProjectPayload
        │
        ▼
 ┌──────────────────────────────────────────────────────┐
 │ PASS 1 — Governance & Entity Integrity   → section 3 │
 │   Stakeholder Register + RACI matrix                 │
 │   ✔ every RACI actor has a register entry            │
 │   ✔ every activity has exactly one A and ≥ one R     │
 │   ✔ district & field operational roles included      │
 └──────────────────────┬───────────────────────────────┘
                        ▼  (gate → re-prompt on failure)
 ┌──────────────────────────────────────────────────────┐
 │ PASS 2 — Technical Stack Matching        → section 4 │
 │   One #### sub-block per tool in the input array     │
 │   ✔ set(input_tools) ⊆ set(documented_tools)         │
 │   ✔ ≥ 4 failure modes across all 4 categories        │
 └──────────────────────┬───────────────────────────────┘
                        ▼  (gate → re-prompt on failure)
 ┌──────────────────────────────────────────────────────┐
 │ PASS 3 — Deep Operational Execution   → sections 5–6 │
 │   Consumes the output of passes 1 and 2              │
 │   ✔ all 4 mandatory phases present                   │
 │   ✔ every phase has real commands, not directives    │
 │   ✔ every phase has explicit error handling          │
 └──────────────────────┬───────────────────────────────┘
                        ▼
        Deterministic assembly (sections 1, 2, 7)
                        ▼
     ██ FINAL VALIDATION GATE (all checks re-run) ██
                        ▼
          Markdown  ──►  .docx  ──►  download
```

Sections 1 (Document Control), 2 (Purpose & Scope) and 7 (Records & Approval)
are assembled in Python, never by the model, so the document's skeleton is
guaranteed regardless of how the model behaves.

### The validation gate

Every check is programmatic Python running against the generated Markdown — no
model is consulted, so the checks cannot themselves hallucinate.

| Gate | Rule | On failure |
| --- | --- | --- |
| **Tool coverage** | `set(input_tools) ⊆ set(mentioned_tools)`, and every tool owns a `#### <Tool>` sub-block | Re-prompt naming the missing tools |
| **RACI completeness** | Every activity row has exactly one `A` and at least one `R` | Re-prompt naming the defective rows |
| **Entity integrity** | Every RACI column has a Stakeholder Register entry; no supplied stakeholder is dropped | Re-prompt naming the unregistered actors |
| **Failure modes** | ≥ 4 rows covering data corruption, connection timeout/deadlock, resource exhaustion, downstream refresh | Re-prompt naming the uncovered categories |
| **Execution depth** | Phases 1–4 all present; each has fenced commands and an error-handling block | Re-prompt; a draft ending at Phase 1 is rejected |

**Escalation ladder.** Each pass gets `MAX_VALIDATION_RETRIES` (default 2)
re-prompts, each one naming the exact defects and the required corrections. If
the model still cannot comply, deterministic Python repair splices in the
missing tool sub-blocks, RACI rows, or phase blocks. Only if *that* fails is the
whole section replaced with a compliant fallback. A document is never released
in a defective state — with `STRICT_VALIDATION` on, the API returns 422 with the
failing gate rather than shipping an incomplete SOP.

### Generated document structure

1. Document Control (ID, revision hash, classification, review date, approval status)
2. Purpose and Scope (purpose, context, scope, out-of-scope, definitions)
3. Governance and RACI Stakeholder Matrix
4. Tooling Architecture and Technical Prerequisites — one sub-block per tool
5. Operating Execution Phases — the four mandatory phases
6. Quality Assurance and Controls
7. Records, Retention and Document History (with a signature block)

**The four mandatory execution phases**, each with exact commands, absolute
paths, verification actions, and error handling:

| Phase | Contains |
| --- | --- |
| 1 — Environment Setup & Health Checks | `docker compose ps`, `psql` connectivity, credential expiry query, disk headroom, health probe |
| 2 — Ingestion & Data Transformation | File-arrival checks, `sha256sum -c`, schema validation, transformation, reconciliation |
| 3 — Database Load & Indexing | Staging truncate, collision detection query, `ON CONFLICT DO UPDATE` promotion, `REINDEX`, explicit `COMMIT`/`ROLLBACK` |
| 4 — Reporting, Dashboards & Export | Gateway service check, dataset refresh endpoint, refresh-status verification, export generation, log publication, sign-off |

---

## Project layout

```
sop-engine/
├── Start SOP Builder.bat        # ← Windows users double-click this
├── start-sop-builder.sh         # ← Mac/Linux users double-click this
├── app/
│   ├── main.py                  # FastAPI: JSON API + serves the web app
│   ├── config.py                # Ollama host, model, retry budget
│   ├── schemas.py               # Pydantic payloads + validation report
│   ├── static/index.html        # The web application (self-contained)
│   ├── agents/
│   │   ├── orchestrator.py      # 3-pass pipeline + gates + repair
│   │   ├── governance_agent.py  # Pass 1
│   │   ├── tech_agent.py        # Pass 2
│   │   └── operations_agent.py  # Pass 3
│   └── services/
│       ├── ollama_client.py     # Local SDK wrapper + health checks
│       ├── markdown_utils.py    # Shared GFM table/section parser
│       ├── validators.py        # The programmatic validation gate
│       └── document_exporter.py # Markdown → styled .docx
├── tests/test_engine.py         # 84 offline tests
├── .env.example
├── Dockerfile
├── requirements.txt
└── sample_payload.json
```

---

## For developers: the API

The web app is a client of the same public API.

### Run manually

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Web app: <http://127.0.0.1:8000> · API docs: <http://127.0.0.1:8000/docs>

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The web application. |
| `GET` | `/api/v1/info` | Service banner, pipeline stages, route index. |
| `GET` | `/api/v1/health` | Ollama reachability and model availability. |
| `POST` | `/api/v1/sop/generate` | Run the 3-pass pipeline; returns Markdown, download URL, validation report. |
| `GET` | `/api/v1/sop/download/{document_id}` | Serve the generated `.docx`. |

### Check the runtime is ready

```bash
curl http://127.0.0.1:8000/api/v1/health
```

### Generate an SOP

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sop/generate -H "Content-Type: application/json" -d @sample_payload.json
```

Response:

```json
{
  "document_id": "SOP-DEPART-20260818-9F2C4A1B",
  "markdown_content": "# Standard Operating Procedure\n...",
  "docx_download_url": "http://127.0.0.1:8000/api/v1/sop/download/SOP-DEPART-20260818-9F2C4A1B",
  "validation": {
    "passed": true,
    "attempts": 3,
    "issues": [],
    "tools_declared": ["Python", "PostgreSQL", "PowerBI", "Docker"],
    "tools_missing": [],
    "phases_found": [1, 2, 3, 4],
    "raci_rows_checked": 9
  }
}
```

> Three sequential passes, plus any retries. On an 8B model expect roughly
> **3–8 minutes** on CPU, or 1–2 minutes with GPU offload. `attempts` tells you
> how many model calls it actually took; anything above 3 means a gate caught a
> defect and re-prompted.

### `ProjectPayload`

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `project_name` | `str` | yes | |
| `department` | `str` | yes | Also seeds the document ID prefix. |
| `description` | `str` | yes | The richer this is, the better the inference. |
| `stakeholders` | `List[StakeholderInput]` | no | `name`, `role`, optional `department`. Defaults to statutory roles if empty. |
| `tools` | `List[str]` | no | Every entry gets its own sub-block in section 4. |
| `security_classification` | `str` | no | Defaults to `"RESTRICTED / INTERNAL"`. |

### Status codes

| Code | Meaning |
| --- | --- |
| `200` | Document generated and validated. |
| `404` | Unknown `document_id` on download. |
| `422` | Payload failed validation, **or** the document failed the structural gate (response includes the full report). |
| `503` | Ollama unreachable or model not pulled — the `hint` says which. |

---

## Configuration

Copy `.env.example` to `.env` only if you need to change a default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Local daemon address. |
| `MODEL_NAME` | `deepseek-r1:8b` | Model used by all three passes. |
| `TEMPERATURE` | `0.2` | Low, for reproducible official text. |
| `NUM_CTX` | `8192` | Context window; raise if sections truncate. |
| `REQUEST_TIMEOUT` | `600` | Seconds per model call. |
| `MAX_VALIDATION_RETRIES` | `2` | Re-prompts per pass before deterministic repair. |
| `STRICT_VALIDATION` | `true` | Reject rather than release a non-compliant document. |
| `OUTPUT_DIR` | `/tmp/sop_engine` | Where `.docx` artifacts are written. |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Used to build the returned download URL. |

### Choosing a model

| Model | Size | Notes |
| --- | --- | --- |
| `deepseek-r1:8b` | ~5 GB | Strongest reasoning; default. Its `<think>` blocks are stripped automatically. |
| `llama3.1:8b` | ~5 GB | Faster, follows table formatting well. |
| `qwen2.5:7b` | ~4.7 GB | Good Markdown discipline. |
| `llama3.2:3b` | ~2 GB | Low-RAM machines. Expect more gate retries. |

---

## Testing

```bash
pytest
```

```
84 passed
```

The suite never contacts Ollama — a fake client returns canned Markdown — so it
runs on a machine with no model installed. It covers: schema validation,
reasoning-block stripping, the shared Markdown parser, all five validation
gates (including deliberately defective inputs), each agent's prompt
construction and repair prompts, three-pass ordering, the retry ladder, every
deterministic repair path, the DOCX converter, and every API route including the
503 and 422 failure paths.

Notable behavioural tests:

- `test_three_separate_llm_passes_are_made` — proves the document is never one-shot.
- `test_document_ending_at_phase_one_is_rejected` — the truncation case.
- `test_persistent_bad_tech_is_repaired_deterministically` — a model that never complies still yields a valid document.
- `test_single_stakeholder_still_yields_r_and_a` — a one-person project cannot break the RACI rules.
- `test_web_app_has_no_external_resources` — the GUI stays air-gapped.

---

## Docker

```bash
docker build -t sop-engine .
```

```bash
docker run -p 8000:8000 -e OLLAMA_HOST=http://host.docker.internal:11434 sop-engine
```

On Linux, add `--add-host=host.docker.internal:host-gateway`, or use
`--network host` with `OLLAMA_HOST=http://127.0.0.1:11434`.

---

## Design notes

**Structure is not left to the model.** Document control, purpose/scope, and the
records/approval block are generated in Python. Only the analytical sections come
from the model, so a bad generation degrades content quality — never document
validity.

**Validation is programmatic, not prompted.** Asking a model to "make sure every
tool is included" is a suggestion. `set(input_tools).issubset(...)` is a
guarantee. Every gate is Python that runs after generation.

**Repairs preserve good content.** When a pass fails, the deterministic repair
splices in only what is missing and keeps what the model got right. Only a
totally unsalvageable section is replaced wholesale.

**Reasoning models are handled.** `deepseek-r1` emits `<think>…</think>` blocks;
these are stripped in the client and again at assembly, so chain-of-thought can
never reach an official document.

**The GUI degrades honestly.** If Ollama is not running, the app says so before
you fill in the form, disables the button, and shows the exact command to fix
it — rather than failing after five minutes of apparent progress.

### Known limitations

- Generated content is a **draft for approval**; it carries no authority until
  countersigned by the Accountable Officer.
- The deterministic phase templates use a departmental standard path and IP
  convention (`/opt/sop/...`, `10.20.0.x`). Where they appear, they must be
  replaced with the values in your asset register — the document says so.
- Artifacts are stored on the local filesystem, so a multi-replica deployment
  needs shared storage.
- Generation is synchronous. The web app holds the request open and shows
  progress; for high volume, move it behind a task queue.
- Progress steps in the GUI advance on typical pass durations, not live server
  events, since generation is a single HTTP request.
- Output quality scales with model size; 3B models trigger noticeably more gate
  retries than 8B ones.

---

## Licence

Open source. Uses only permissively licensed dependencies (FastAPI, Pydantic,
python-docx, Ollama) and locally hosted open-weight models.
