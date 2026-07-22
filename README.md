# Resume Screening & Talent Matching Agent with Bias Mitigation

An AI pipeline that ingests a job description plus a directory of PDF resumes,
**anonymizes demographic indicators before any text reaches the model**,
semantically evaluates candidates with the Claude API, and produces a ranked
HTML dashboard with per-candidate justifications and tailored interview questions.

## Run with Docker (recommended)

```powershell
docker compose up
```

That's the whole setup. One command brings up Postgres, Redis, the web app,
and the background worker; migrations run automatically; an admin account
(`admin` / `admin123` by default — change it after first login) is created
automatically if none exists yet. No `pip install`, no manual migration
step, no separate bootstrap command.

Then open http://localhost:5000, and log in at `/admin/login` with the admin
credentials above.

**Services** (`docker-compose.yml`): `db` (Postgres 16), `redis` (Redis 7),
`migrate` (one-shot: `alembic upgrade head` + idempotent admin bootstrap —
web/worker wait for this to finish before starting), `web` (gunicorn), `worker`
(the RQ background processor — see [Background processing](#background-processing)).
Uploaded PDFs are shared between `web` and `worker` via a named volume
(`uploads_data`), since they're separate containers but both need the same
files. Postgres data persists in the `postgres_data` volume across restarts.

Want to use Claude instead of the free Ollama fallback, or change the demo
admin password? Copy `.env.example` to `.env` and edit it —
`docker-compose.yml` reads it automatically; every value has a working
default already baked in, so `.env` is optional, not required.

Running Ollama on your own machine (not in Docker)? The `web`/`worker`
containers reach it at `http://host.docker.internal:11434` automatically
(see `OLLAMA_BASE_URL` in `screener/evaluate_ollama.py`) — no config needed
on Docker Desktop; on native Linux Docker Engine, `docker-compose.yml`
already sets the required `host-gateway` mapping.

## Demo web portal (running without Docker)

A small Flask app in `webapp/` lets a candidate upload a resume PDF through a
browser instead of the CLI. The homepage lists every **active** job position
(whatever recruiters have created/opened from the dashboard's Job management
page) in a picker — candidates choose which role they're applying for, and
the page updates to show that role's summary and required skills. Uploading
returns **immediately** — parsing, anonymizing, and evaluating all happen in
a background worker process, and the page polls for status until it's done.
Uses `--backend auto` behavior: Claude if `ANTHROPIC_API_KEY` is set,
otherwise the free local Ollama model automatically.

The candidate never sees the AI's evaluation (score, justification, gaps,
interview questions) — only that their submission was received. That detail
is reserved for recruiters/admins in the dashboard and API, gated by the
`VIEW` permission (see [Authentication & security](#authentication--security)).

All submitted-application data (applicants, resumes, evaluations, bias-audit
logs, processing-job status) is stored in **Postgres**, not JSON files — see
[Database](#database) below to provision it before running the app. Job
queuing needs **Redis** — see [Background processing](#background-processing).

```powershell
pip install -r requirements.txt
python webapp/app.py          # terminal 1 — the web server
python webapp/worker.py       # terminal 2 — the background worker
```

Then open http://localhost:5000. Log in at `/admin/login` for the
[recruiter dashboard](#recruiter-dashboard) — see [Authentication & security](#authentication--security)
to bootstrap your first account.

## Recruiter dashboard

`/admin/dashboard` — overview stats (total applicants, active jobs, completed
evaluations, processing queue), a searchable/filterable/sortable candidate
table, per-candidate detail pages, job-position management, and CSV/JSON
export.

- **Search & filter**: by name/filename, job position, recruiter status
  (New/Shortlisted/Interview/Rejected), or pipeline status; sort by date,
  score, or name.
- **Candidate detail** (`/admin/candidates/<id>`): the original PDF (unmodified,
  for recruiter review), the full evaluation (overall/skills/experience/project-impact
  scores, justification, gaps, interview questions), the bias-mitigation audit
  table, and — new in this schema — the actual **anonymized resume text** the
  model was shown. Raw (pre-redaction) text is still never persisted anywhere,
  matching the pipeline's hash-only audit philosophy.
- **Ranking actions**: shortlist / mark for interview / reject, from either
  the table or the detail page — instantly, via `POST /admin/candidates/<id>/status`.
- **Job management** (`/admin/jobs`): create and edit positions (title,
  description, required skills, minimum experience, active/closed status).
  Every **active** position immediately appears in the public apply page's
  job picker; closing a position removes it from the picker (existing
  applications for it are unaffected).
- **Export**: `/admin/export.csv` and `/admin/export.json`, honoring whatever
  search/filter/sort is currently applied.

> **Score dimensions note:** the AI evaluation produces Skill Match,
> Experience Relevance, Project Impact, and a computed Overall — there is no
> "Education" dimension in the pipeline's prompt/schema (`screener/evaluate.py`,
> `screener/evaluate_ollama.py`). The dashboard shows the three real dimensions
> rather than fabricating an education score. Adding a genuine 4th dimension
> would mean changing the LLM prompt/schema and re-weighting `compute_overall`
> in `screener/models.py` — a deliberate decision, not done here.

Then open http://localhost:5000.

## Authentication & security

There is no shared password anymore. Every recruiter/admin/viewer is a real
account (`webapp/models_db.py` `User`) with a hashed password
(`werkzeug.security.generate_password_hash` — scrypt-based, salted, never
reversible — never plaintext). Session management is Flask-Login;
CSRF protection is Flask-WTF's `CSRFProtect`, applied to every POST route.

**Bootstrap the first account** (there's a chicken-and-egg problem otherwise —
nothing exists yet to log in with):

```powershell
python scripts/create_user.py --username admin --role admin
```

You'll be prompted for a password (min 8 characters). Once at least one admin
exists, more accounts (any role) can be created from `/admin/users` instead.

**Roles** (`webapp/auth.py`'s `ROLE_PERMISSIONS`):

| Role | view | manage candidates<br>(status, delete one) | manage jobs<br>(create/edit) | delete all | manage users |
|---|---|---|---|---|---|
| **Admin** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Recruiter** | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Viewer** | ✅ | ❌ | ❌ | ❌ | ❌ |

Every route checks `require_permission(...)` — not just "logged in" — and
returns a real `403` if the role lacks the permission, regardless of what the
UI shows. Templates independently hide controls a role can't use (via the
same `has_permission()` check), so the UI never offers a button that would
just fail, but the enforcement is server-side either way.

**Input validation**: username (3-64 chars, `[a-zA-Z0-9_.-]`), password (min 8
chars), role (must be `admin`/`recruiter`/`viewer`) in `webapp/auth.py`;
job-position fields (non-empty title, non-negative years of experience,
status must be `active`/`closed`) in `webapp/recruiter.py`.

**Environment secrets**: `FLASK_SECRET_KEY` is required (the app refuses to
start without it) — it signs both session cookies and CSRF tokens. There is
deliberately no auto-generated fallback: a randomly-regenerated key would
invalidate every session on restart and differ across worker processes in a
real multi-process deployment, silently breaking logins in a way that's easy
to miss in development and painful to debug in production.

## Testing

```powershell
pip install -r requirements-dev.txt
python -m spacy download en_core_web_sm   # only needed once, for anonymization NER tests
pytest
```

Runs automatically on every push/PR via `.github/workflows/tests.yml` — no
external services required in CI: the test DB is a throwaway per-session
SQLite file (`tests/conftest.py` sets `DATABASE_URL` before any `webapp`
module is imported), and Redis/AI calls are always mocked, never dialed for
real. Config lives in `pytest.ini` (`--cov-fail-under=80`) and `.coveragerc`.

**124 tests, ~90% coverage** across `screener/` and `webapp/`:

| | |
|---|---|
| `tests/unit/test_ingest.py` | PDF text extraction (real PDFs via reportlab, not mocked), batch resume loading, JD loading (JSON + plain text) |
| `tests/unit/test_anonymize.py` | every redaction rule (email/phone/university/URL/honorific/DOB), pronoun neutralization, tech-term whitelist, NER person/location redaction, residual-pass name catching, hash-only audit trail |
| `tests/unit/test_scoring.py` | `compute_overall`'s weighted-average formula |
| `tests/unit/test_evaluate.py`, `test_evaluate_ollama.py` | both AI backends — success, refusal, rate-limit retry, malformed response, connection/timeout errors — all via mocked `anthropic`/`requests` calls, no API key or running Ollama needed |
| `tests/unit/test_report.py` | HTML card rendering (incl. HTML-escaping), JSON/HTML report writing, ranking order |
| `tests/integration/test_upload_flow.py` | `POST /apply` — immediate response, DB rows created, job enqueued (mocked queue) |
| `tests/integration/test_processing_flow.py` | the full worker pipeline (`webapp/tasks.process_resume`) against real PDFs with mocked AI calls — success path, no-backend-available, corrupt-PDF (non-retryable), and the retry/exhausted-retry logic |
| `tests/integration/test_dashboard_endpoints.py` | auth gating, all 3 roles' permission boundaries (including that a 403 happens server-side, not just a hidden button), search/filter, job & user management, CSV/JSON export, and a dedicated test proving CSRF is actually enforced |

## Database

The webapp persists to Postgres via SQLAlchemy models (`webapp/models_db.py`)
and Alembic migrations (`migrations/`). Eight tables: `users`, `recruiters`,
`job_positions`, `applicants`, `resumes`, `evaluations`, `audit_logs`
(bias-mitigation redaction records + admin actions), and `processing_jobs`
(evaluation run status — the seam for future background-job processing).

**1. Start Postgres** (local dev — swap credentials for prod):

```powershell
docker run -d --name screener-db -e POSTGRES_USER=screener `
  -e POSTGRES_PASSWORD=screener -e POSTGRES_DB=screener -p 5432:5432 postgres:16
```

**2. Point the app at it** — set `DATABASE_URL` in `.env` (see `.env.example`):

```
DATABASE_URL=postgresql+psycopg2://screener:screener@localhost:5432/screener
```

**3. Apply migrations:**

```powershell
python -m alembic upgrade head
```

`webapp/app.py` also calls `init_db()` on startup as a dev convenience
(`create_all`, idempotent) — but the source of truth for schema changes is
Alembic; run `alembic revision --autogenerate -m "..."` after editing
`webapp/models_db.py` and commit the generated migration.

No Postgres available (e.g. a quick local smoke test)? Set
`SCREENER_ALLOW_SQLITE=1` instead of `DATABASE_URL` — never use this in
production.

## Background processing

Resume evaluation (the slow part — an LLM call that can take anywhere from a
few seconds to a few minutes) never runs inside a Flask request. `/apply`
saves the upload, writes `pending` `Resume`/`ProcessingJob` rows, enqueues an
RQ job, and returns immediately. `webapp/worker.py` is a separate process
that pulls jobs off the queue and runs the real pipeline (parse → anonymize →
evaluate → render) via `webapp/tasks.py`, updating the job's status/progress
as it goes. The result page and the admin dashboard both poll
`GET /status/<public_id>` until the job reaches a terminal state.

**1. Start Redis:**

```powershell
docker run -d --name screener-redis -p 6379:6379 redis:7-alpine
```

**2. Set `REDIS_URL` in `.env`** (see `.env.example`):

```
REDIS_URL=redis://localhost:6379/0
```

**3. Run the worker** alongside the web server:

```powershell
python webapp/worker.py
```

**Status lifecycle:** `pending` → `processing` → `completed` | `failed`.
Progress (0–100) and a short human-readable message update at each pipeline
stage (`Reading PDF`, `Anonymizing`, `Evaluating via <backend>`, `Saving
results`).

**Retries & error recovery:** every job gets 3 attempts (`webapp/jobs.py`'s
`DEFAULT_RETRY`, backing off 10s/30s/60s). A parse error (corrupt PDF, no
extractable text) is treated as permanently failed immediately — retrying
can't fix a bad file. Any other unexpected exception (a transient DB hiccup,
a bug) is retried up to the attempt budget; once exhausted, the job and its
resume are marked `failed` with the error recorded, and the UI stops polling
and shows it. `ProcessingJob.attempts` tracks how many runs it took.

**Windows note:** RQ's default `Worker` forks a child process per job
(`os.fork()`, POSIX-only). `webapp/worker.py` automatically uses
`SimpleWorker` (same-process execution, no fork) on Windows and the real
forking `Worker` everywhere else — Linux/Docker deployments get full per-job
crash isolation.

## Logging & monitoring

Every process (the web app, the worker, and the `migrate`/bootstrap scripts)
logs structured JSON to stdout via `webapp/logging_config.py` — one line per
event, ready for any log aggregator (`docker compose logs`, CloudWatch, Loki,
etc.) without a separate parsing step:

```json
{"timestamp": "2026-07-20T22:57:38+0400", "level": "INFO", "logger": "webapp.tasks",
 "message": "ai_response_time", "request_id": "-", "processing_id": "job-1",
 "resume_id": 1, "job_id": 1, "backend": "ollama", "ai_duration_ms": 12753.4}
```

- **Request IDs**: every HTTP request gets one (reused from an incoming
  `X-Request-ID` header if present, otherwise generated), bound via a
  `contextvar` so every log line emitted while handling that request carries
  it automatically — no need to thread it through every function call. It's
  also echoed back as the `X-Request-ID` response header, so a user-reported
  error can be traced straight to its server-side log lines.
- **Processing IDs**: every background job gets `job-<id>` bound the same
  way at the top of `webapp/tasks.process_resume`, so every log line for one
  resume's processing — across parsing, anonymizing, evaluating, saving —
  can be grepped out even with many jobs interleaved in the worker's log
  stream.
- **Levels**: INFO for normal lifecycle events (`request_started`,
  `resume_submitted`, `processing_completed`, `login_succeeded`, ...),
  WARNING for recoverable/expected problems (`parsing_failed` — a bad PDF,
  not retryable but not a bug either — `login_failed`, `delete_all_candidates`),
  ERROR for real failures (`evaluation_failed`, `processing_failed`, with a
  full traceback via `exc_info=True` on unexpected exceptions).
- **Timing tracked explicitly**: `processing_completed`/`processing_failed`
  carry `duration_ms` for the whole pipeline run; `ai_response_time`/
  `evaluation_failed` separately carry `ai_duration_ms` for just the
  Claude/Ollama call, so a slow resume can be attributed to "the model was
  slow" vs. "something else in the pipeline was slow" at a glance.
- **Parsing and evaluation failures** are always logged (`parsing_failed` at
  WARNING, `evaluation_failed` at ERROR) with the actual error message, in
  addition to being recorded on the `Resume`/`Evaluation` rows for the UI.

Set `LOG_LEVEL` (default `INFO`) to change verbosity; `docker-compose.yml`
already wires it through to both `web` and `worker`.

## REST API

A JSON API lives under `/api/*` (`webapp/api.py`) alongside the HTML
dashboard, for programmatic access to the same data — jobs, applicants,
evaluations, and submissions.

**Auth**: the API reuses the app's existing session-cookie login rather than
a separate token scheme — one auth system, not two. A client logs in the
same way a browser does (`POST /admin/login` with `username`/`password`),
then sends the resulting session cookie on subsequent `/api/` calls. This is
a deliberate scope decision for a project this size, not an oversight; a
pure machine-to-machine deployment would more typically want a bearer
token instead. CSRF protection is exempted for this blueprint (form-token
CSRF doesn't fit non-browser JSON/multipart clients) — safe here because
every mutating endpoint requires an explicit JSON or multipart body, not
something a plain cross-site HTML form can forge.

Every route enforces the same role permissions as the dashboard
(`VIEW` / `MANAGE_JOBS` / `MANAGE_CANDIDATES`, see [Authentication &
security](#authentication--security)), but reports a JSON `401`/`403`
instead of redirecting to the login page.

**Endpoints**:

| Method | Path                  | Permission        | Notes                                   |
|--------|-----------------------|--------------------|------------------------------------------|
| GET    | `/api/jobs`           | VIEW               | paginated, `?status=active\|closed`      |
| POST   | `/api/jobs`           | MANAGE_JOBS        | create a job position                    |
| GET    | `/api/jobs/<id>`      | VIEW               | 404 if unknown                           |
| POST   | `/api/resumes`        | MANAGE_CANDIDATES  | multipart upload, enqueues evaluation    |
| GET    | `/api/resumes/<id>`   | VIEW               | candidate detail (by public id)          |
| GET    | `/api/applicants`     | VIEW               | paginated, search/filter/sort            |
| GET    | `/api/evaluations`    | VIEW               | paginated, only resumes with a completed evaluation |
| GET    | `/api/dashboard`      | VIEW               | overview stats                           |

**Pagination**: list endpoints accept `?page=` (default 1) and `?per_page=`
(default 20, capped at 100) and respond with:

```json
{"data": [...], "pagination": {"page": 1, "per_page": 20, "total": 42, "total_pages": 3}}
```

**Filtering**: `/api/jobs?status=active|closed`; `/api/applicants?q=<search>&status=<recruiter status>&job_id=<id>`;
`/api/evaluations?backend=<claude|ollama>&min_score=<0-100>&max_score=<0-100>&job_id=<id>`.

**Errors** always come back as JSON, including 404s and 405s on unknown
`/api/*` routes:

```json
{"error": {"code": "validation_error", "message": "Invalid job position payload.", "fields": {"title": "required"}}}
```

Example:

```bash
curl -c cookies.txt -X POST http://localhost:5000/admin/login -d "username=admin&password=..."
curl -b cookies.txt "http://localhost:5000/api/applicants?status=new&per_page=10"
```

## Pipeline

```
PDFs + JD ──> 1. Ingestion (pdfplumber)
          ──> 2. Anonymization (spaCy NER + regex) ── redaction audit
          ──> 3. Semantic evaluation (Claude, structured JSON output)
          ──> 4. output/report.html + output/results.json
```

**Bias mitigation:** names, locations, universities, emails, phones, links,
addresses, birth dates, graduation years, honorifics, and (by default) gendered
pronouns are replaced with placeholders. Only the anonymized text is sent to the
model, and the model is instructed to ignore prestige signals. Each candidate
card shows an audit of what was redacted (counts + hashes — never raw values).

## Setup

```powershell
cd resume-screener
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
copy .env.example .env       # works as-is with local Ollama; uncomment ANTHROPIC_API_KEY in .env for Claude instead
```

## Test data

**Option A — synthetic (built in, no download):**

```powershell
python scripts/make_sample_resumes.py
```

Generates 10 fake resume PDFs into `data/resumes/` with planted demographic
markers (names, cities, elite vs. unknown universities) deliberately decoupled
from skill quality — so you can verify the anonymizer strips every marker and
the ranking tracks skill, not demographics.

**Option B — real PDFs from Kaggle:**
Download the [Resume Dataset](https://www.kaggle.com/datasets/snehaanbhawal/resume-dataset)
(~2,400 real resume PDFs in 24 job-category folders) and copy a subset, e.g. the
`INFORMATION-TECHNOLOGY/` folder, into `data/resumes/`. Use `--limit` to cap cost.

## Run

```powershell
python -m screener --jd data/job_description.json --resumes data/resumes --top 10
```

Then open `output/report.html` in a browser.

Useful flags:

| Flag | Purpose |
|---|---|
| `--limit 25` | Only process the first 25 PDFs (cost control on big datasets) |
| `--no-eval` | Stop after anonymization — no API calls, no key needed |
| `--dump-anonymized` | Write the exact anonymized payloads to `output/anonymized/` for auditing |
| `--model claude-sonnet-5` | Choose the Claude model |
| `--keep-pronouns` | Skip pronoun neutralization |

## Verifying the anonymization

```powershell
python scripts/make_sample_resumes.py
python -m screener --jd data/job_description.json --resumes data/resumes --no-eval --dump-anonymized
# every planted marker must be absent from output/anonymized/*.txt
findstr /i /g:data\resumes\_planted_markers.txt output\anonymized\*.txt
```

No output from `findstr` = no demographic markers leaked.

## Cost notes

One evaluation call per resume (~2–4k input tokens each). The JD + rubric are
prompt-cached, so repeated calls reuse the cached prefix. A 10-resume run costs
a few cents on `claude-sonnet-5`.
