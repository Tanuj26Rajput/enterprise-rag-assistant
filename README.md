# Enterprise Multi-Document RAG Assistant

A multi-tenant retrieval-augmented generation service. Teams upload documents
into isolated workspaces, ask questions in natural language, and get answers
that **cite the passages they came from** — or say plainly that the documents
do not contain an answer.

FastAPI · PostgreSQL · ChromaDB · OpenRouter · Streamlit

---

## What it does

- **Multi-tenant by construction.** Workspaces (namespaces) with four roles.
  Every query is tenant-filtered, every vector carries its tenant triple, and
  non-membership returns 404 so namespace ids cannot be enumerated.
- **Ingests PDF, DOCX, TXT and Markdown.** Validated by magic bytes, not by
  file extension. Extracted with page numbers where the format has them,
  chunked on token boundaries with overlap.
- **Answers with citations that cannot be invented.** The model emits
  bracketed markers and nothing else; this service resolves them against the
  passages it actually supplied. A marker naming a passage that was not
  supplied is deleted from the answer. Document names, pages and excerpts
  come from the database.
- **Says "I don't know".** When the retrieved passages do not support an
  answer, the response says so instead of composing something plausible.
- **Streams.** Server-sent events, with citations in the final event, correct
  handling of client disconnects and in-band reporting of upstream failures.
- **Survives failure.** Ingestion is a durable job in PostgreSQL with leases,
  bounded retries, exponential backoff, dead-lettering and crash recovery.
  Permanent failures are not retried three times.
- **Is observable.** Structured JSON logs with a request id threaded through
  every line, Prometheus metrics, and separate liveness and readiness probes.

---

## Quick start

### Docker

```bash
cp .env.example .env     # set SECRET_KEY and one provider key
docker compose up --build
```

→ UI on <http://localhost:8501>, API docs on <http://localhost:8000/docs>.

> The image builds, and all four roles (`migrate`, `api`, `worker`,
> `frontend`) have been run against a real PostgreSQL — see
> [docs/audit.md](docs/audit.md#verified-in-containers-2026-10-23) for exactly
> what was checked. `docker compose up` as a whole has **not** been run: give
> it a `.env` that does not point at a database you care about, because the
> `migrate` service applies pending migrations on start.

### Local

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env
alembic upgrade head          # the application never creates tables

python run.py                 # API      → http://127.0.0.1:8000/docs
streamlit run frontend/app.py # UI       → http://localhost:8501
python -m app.worker          # only if INGESTION_MODE=background
```

`run.py` exists for Windows, where psycopg's async driver cannot run on the
default event loop. On Linux, `uvicorn app.main:app` works directly.

### Two minutes to a cited answer

```bash
API=http://127.0.0.1:8000/api/v1

TOKEN=$(curl -s -X POST $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-battery-staple"}' >/dev/null &&
  curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-battery-staple"}' | jq -r .access_token)

NS=$(curl -s -X POST $API/namespaces -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"slug":"acme","name":"Acme"}' | jq -r .id)

COL=$(curl -s -X POST $API/namespaces/$NS/collections -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"slug":"handbook","name":"Handbook"}' | jq -r .id)

curl -s -X POST $API/namespaces/$NS/collections/$COL/documents \
  -H "Authorization: Bearer $TOKEN" -F "file=@handbook.pdf"

curl -s -X POST $API/namespaces/$NS/collections/$COL/answer \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"How much annual leave carries over?"}' | jq '{answer, citations}'
```

---

## How it is put together

```
Streamlit ──HTTP──▶ FastAPI ──▶ PostgreSQL   tenancy · documents · jobs
(no secrets)                 ├─▶ ChromaDB     embeddings + metadata
                             ├─▶ Blob store   uploaded originals
                             ├─▶ OpenAI       embeddings
                             └─▶ OpenRouter   answers
                   Worker ───▶ (the same stores; claims jobs with SKIP LOCKED)
```

Diagrams — architecture, components, ER, sequences and deployment — are in
**[docs/architecture.md](docs/architecture.md)**.

```
app/
  api/          routers, dependencies, SSE framing
  core/         config, security, exceptions, logging, metrics, retry
  db/           SQLAlchemy models and session management
  repositories/ data access; every method is tenant-scoped
  services/     all business logic and every authorization decision
  ingestion/    validation, extraction, chunking
  embeddings/   provider abstraction + model registry
  llm/          OpenAI-compatible client, prompts
  vectorstore/  ChromaDB adapter behind an 8-method protocol
  worker.py     the ingestion worker
frontend/       Streamlit UI — talks only to the API
migrations/     Alembic
tests/          494 tests
docs/           architecture · api · environment · trade-offs · troubleshooting · audit
```

**The layering rule:** routers parse and delegate, services decide,
repositories query, adapters talk to the outside. Nothing calls upward. Every
namespace-scoped call starts at `NamespaceAuthorizer`, so there is no path to
a tenant's data that skips the check.

---

## Three decisions worth knowing before you read the code

**The queue is a PostgreSQL table.** Workers claim with
`FOR UPDATE SKIP LOCKED` and hold a lease. No Redis, no broker — because the
job and the document row it concerns commit in *one transaction*, so "job
succeeded but the document was never marked ready" is a state this design
cannot reach.

**Citations are resolved by the application, never by the model.** The
failure mode of a RAG system is not a wrong answer — a reader can judge
that. It is a plausible answer with a citation that looks real. Markers are
the only citation syntax the model is given, so that failure is unreachable.

**Embeddings are configured independently of chat.** OpenRouter fronts *chat*
models; most have no embeddings endpoint. The shortcut — "they configured
OpenRouter, so embed through OpenRouter" — produces a deployment that accepts
uploads and then fails on the first one.

Each of these, with the rejected alternative and the price being paid, is in
**[docs/trade-offs.md](docs/trade-offs.md)**.

---

## Configuration

Three variables have no sensible default:

| Variable | |
|---|---|
| `DATABASE_URL` | `postgresql://user:pass@host:5432/db` |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `OPENROUTER_API_KEY` *or* `OPENAI_API_KEY` | Uploads and search work without one; answering returns 503 |

Everything else has a default and is documented in
**[docs/environment.md](docs/environment.md)**. The two you are most likely to
change:

- `INGESTION_MODE` — `inline` (default; one process, upload response reports
  the real outcome) or `background` (a worker claims the job; what a large
  corpus needs).
- `EMBEDDING_PROVIDER` — `openai`, `openrouter`, or `hash` for deterministic
  offline embeddings. `hash` is what lets the entire test suite run with no
  network and no bill; it is refused in production.

`ENVIRONMENT=production` refuses to start with a weak `SECRET_KEY`, `DEBUG`
on, a wildcard `CORS_ORIGINS`, or hash embeddings.

### Two configuration traps worth naming

**A collection's embedding width is fixed when it is created.** Changing
`EMBEDDING_MODEL` to a model of a different width does not migrate anything;
it strands every collection built with the old one. Decide before you index.
Models the registry knows are listed in `app/embeddings/registry.py`, each
with its width stated — including `nvidia/nemotron-3-embed-1b` at 2048. An
OpenRouter model that is not listed needs `EMBEDDING_DIMENSIONS` set
explicitly, which is you vouching for the number; it is checked against the
first real response before anything is indexed.

**Reasoning models need a much larger `LLM_MAX_TOKENS` than they look like
they do.** The budget covers reasoning tokens as well as the answer, and the
reasoning comes first. Measured: `stepfun/step-3.5-flash` given 16 tokens
spent 15 of them thinking and returned `content: null`, which becomes an
empty answer rather than an error — the failure looks like a broken
application, not an exhausted budget. `stepfun/step-3.7-flash` and
`anthropic/claude-sonnet-5.5` use no reasoning tokens and answer correctly,
citations intact, at the default 1024.

---

## Testing

```bash
pytest -q                      # 494 passed
ruff check . && ruff format --check .
```

Tests run against a **real** ChromaDB on a temp directory with real
(deterministic) embeddings. The SQL layer is faked and the chat provider is
scripted — everything else is the real code path. The reasoning behind that
split, and its cost, is in
[docs/trade-offs.md](docs/trade-offs.md#testing-real-chroma-fake-sql-scripted-model).

---

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness — consults nothing. Wire this to the liveness probe. |
| `GET /ready` | Readiness — checks PostgreSQL and the vector store. Wire this to the readiness probe. |
| `GET /metrics` | Prometheus exposition. Internal only. |

Using `/ready` for liveness turns a database failover into a fleet-wide
restart loop, which is why they are separate endpoints with separate
semantics.

Logs are JSON by default, with a `request_id` on every line that also appears
in the `X-Request-ID` header and in the audit table — one id joins the HTTP
response, the log, and the audit row.

---

## Known limits

Stated here rather than discovered later. Full detail in
[docs/audit.md](docs/audit.md#known-gaps).

- **Embedded ChromaDB is single-writer.** SQLite-backed: one writing process,
  local filesystem, no replication, no auth. Hence `WEB_CONCURRENCY=1` and
  one worker replica. `CHROMA_HOST` is the first step out.
- **Tenant isolation is enforced in application code**, not by PostgreSQL
  row-level security.
- **No rate limiting.** Deploy behind something that provides it.
- **No test runs against live PostgreSQL** — the SQL layer is faked. This is
  the costliest gap in the project: running the image against a real database
  immediately exposed four 500s that the whole suite passes over. Treat a
  green suite as no evidence at all that a write path works.
- **No refresh-token revocation.** Signing out discards the client's copy;
  the token stays valid until it expires.
- **The migrated Supabase schema has not been read back.** `0002` and `0003`
  applied on 2026-10-23 and Alembic reports head, but Alembic reports the
  revision it recorded, not the DDL it produced. One query confirms it —
  [docs/audit.md](docs/audit.md#verifying-the-live-schema).

---

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Diagrams and the reasoning behind each boundary |
| [docs/api.md](docs/api.md) | The HTTP surface, the error contract, the SSE client contract |
| [docs/environment.md](docs/environment.md) | Every variable, and what happens when it is wrong |
| [docs/trade-offs.md](docs/trade-offs.md) | Each decision, its rejected alternative, and its price |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Organised by symptom |
| [docs/audit.md](docs/audit.md) | What was found, fixed, and left open |
