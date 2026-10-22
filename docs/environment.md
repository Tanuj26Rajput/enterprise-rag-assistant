# Environment and setup

> Written for whoever deploys or runs this. Every variable is listed with its
> default and — more usefully — with what happens when it is wrong, because
> that is the column you actually need at 2am.

Configuration is read once, at import, by `app/core/config.py` and validated
by Pydantic. Nothing else in the codebase reads `os.environ`. A deployment
with invalid configuration therefore fails at start-up with a specific
message, rather than halfway through the first upload.

A blank value (`KEY=`) is treated as *unset*, not as the empty string. A
template `.env` lists every key, and most deployments leave most of them
empty; without that rule each one would fail validation against its own type
and the operator would be told their configuration is invalid when they
simply have not set it.

---

## Quick start (local, no Docker)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # then edit it — see the table below
alembic upgrade head               # creates the schema; the app never does

python run.py                      # API on http://127.0.0.1:8000/docs
python -m app.worker               # only if INGESTION_MODE=background
streamlit run frontend/app.py      # UI on http://localhost:8501
```

`run.py` exists for Windows only: psycopg's async driver cannot run on the
`ProactorEventLoop`, and uvicorn picks its loop through a factory that
`asyncio.set_event_loop_policy` does not affect. On Linux, `uvicorn
app.main:app` works directly and `run.py` is unnecessary.

## Quick start (Docker)

```bash
cp .env.example .env               # SECRET_KEY and a provider key are required
docker compose up --build
```

This brings up PostgreSQL, runs migrations to completion, then starts the
API, one worker and the frontend. See [architecture §5](architecture.md#5-deployment)
for the topology and its one scaling caveat.

---

## The three variables you must set

| Variable | Why it cannot be defaulted |
|---|---|
| `DATABASE_URL` | There is no sensible default for someone else's database. |
| `SECRET_KEY` | Signs every JWT. The built-in placeholder is refused in production, and anything under 32 characters is refused too. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `OPENAI_API_KEY` *or* `OPENROUTER_API_KEY` | Without one, uploads still work and search still works; answering returns a 503. |

---

## Application

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` | `Enterprise RAG Assistant` | Also the PostgreSQL `application_name`, so it appears in `pg_stat_activity`. |
| `APP_VERSION` | `0.1.0` | A label on `rag_build_info`; set it from your CI tag to tell two versions apart during a rollout. |
| `ENVIRONMENT` | `development` | `production` switches on the hardening checks below and hides `/docs` and `/openapi.json`. |
| `DEBUG` | `false` | Refused in production. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `true` | `false` gives human-readable console output. Containers should leave it on. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Loopback by default so a dev server is not exposed to the local network by accident. The container image sets `0.0.0.0` deliberately — there the container is the boundary. |
| `RELOAD` | `false` | Development only. |
| `CORS_ORIGINS` | `*` | Comma-separated or a JSON array. A wildcard is **refused in production**: with `allow_credentials=true` it would let any site drive the API as a signed-in user. |

### What `ENVIRONMENT=production` enforces

Set it and the process refuses to start if `SECRET_KEY` is the placeholder or
shorter than 32 characters, `DEBUG` is on, `CORS_ORIGINS` contains `*`, or
`EMBEDDING_PROVIDER=hash`. The last one is the easiest to deploy by accident
and the hardest to notice: hash embeddings are syntactic, so a production
collection built on them retrieves plausible-looking nonsense and raises no
error at any point.

## Security

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | *(placeholder)* | Rotating it invalidates every outstanding token — which is the point, during an incident. |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | |
| `PASSWORD_MIN_LENGTH` | `12` | Length only. No composition rules: they push users toward `Password1!` and are not what makes a password strong. |

## Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | *(required)* | `postgresql://user:pass@host:5432/db`. The driver is forced to `postgresql+psycopg`, so write the plain scheme. |
| `DATABASE_DIRECT_URL` | unset | A non-pooled connection for Alembic. DDL and session advisory locks are unreliable through a transaction pooler. |
| `DB_POOL_SIZE` | `10` | Ignored on a transaction pooler — see below. |
| `DB_MAX_OVERFLOW` | `5` | |
| `DB_POOL_TIMEOUT` | `30` | Seconds to wait for a connection before failing the request. |
| `DB_POOL_RECYCLE` | `1800` | Below any proxy or firewall idle timeout, or you get connections that look alive and are not. |
| `DB_POOL_PRE_PING` | `true` | One cheap round trip per checkout; much cheaper than a 500. |
| `DB_CONNECT_TIMEOUT` | `10` | |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | Applied via `options` so it survives a pooler recycling the connection. |
| `DB_ECHO` | `false` | Logs every statement. Development only. |

**The port decides the pooling mode, not the hostname.** Supabase serves both
from `*.pooler.supabase.com`: **6543** is Supavisor/PgBouncer in *transaction*
mode and **5432** is session mode. On 6543 the application switches to
`NullPool` and disables server-side prepared statements, because a connection
is handed to another client between transactions and neither survives that.
Point `DATABASE_URL` at 6543 for containers and serverless; point
`DATABASE_DIRECT_URL` at 5432 so migrations get a real session.

## LLM (answer generation)

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | Or `openai`. |
| `LLM_MODEL` | `anthropic/claude-sonnet-5.5` | OpenRouter uses the `vendor/model` form. |
| `LLM_API_KEY` | unset | Wins over the provider-specific keys below. |
| `OPENROUTER_API_KEY` | unset | Used only when `LLM_PROVIDER=openrouter`. |
| `OPENAI_API_KEY` | unset | Used only when `LLM_PROVIDER=openai`. |
| `LLM_BASE_URL` | provider default | |
| `LLM_TEMPERATURE` | `0.1` | This is extractive QA over supplied passages. Creative variation here is a defect. |
| `LLM_MAX_TOKENS` | `1024` | |
| `LLM_REQUEST_TIMEOUT` | `60` | |
| `LLM_MAX_ATTEMPTS` | `3` | Retries apply to transient failures only. |
| `OPENROUTER_SITE_URL`, `OPENROUTER_APP_NAME` | unset | Attribution headers, sent to OpenRouter only. |
| `ANSWER_EXCERPT_CHARS` | `1200` | A second ceiling on prompt size, so a large `top_k` cannot quietly produce an enormous request. |

**Keys never cross providers.** An `OPENAI_API_KEY` is never sent to
OpenRouter and vice versa. Two credentials sitting in the same `.env` is not
consent to hand one of them to the other vendor.

## Embeddings

Configured **independently of the chat model**, and the separation is
deliberate. Routing chat through OpenRouter says nothing about where
embeddings should go: OpenRouter fronts chat models, and most of them have no
embeddings endpoint at all.

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDING_PROVIDER` | `openai` | `openai`, `openrouter` or `hash`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must match the provider's naming. `openai/text-embedding-3-small` with `EMBEDDING_PROVIDER=openai` is a start-up error, and the message says exactly that. |
| `EMBEDDING_API_KEY` | unset | Falls back to `OPENAI_API_KEY` *only* when the provider is `openai`. |
| `EMBEDDING_DIMENSIONS` | from the registry | Set it to shorten a model that supports shortening, or to vouch for a model the registry does not know. Checked against the first real response. |
| `EMBEDDING_BATCH_SIZE` | `64` | |
| `EMBEDDING_MAX_ATTEMPTS` | `3` | |
| `EMBEDDING_REQUEST_TIMEOUT` | `60` | |

**A collection's embedding model and width are immutable.** They are recorded
on the collection when it is created and refused if they later disagree.
Mixing two models' vectors in one index does not fail — it silently returns
nonsense, because the distances are no longer comparable. Changing the model
means creating a new collection and re-uploading.

`EMBEDDING_PROVIDER=hash` produces deterministic, offline, *syntactic*
embeddings. It makes the whole test suite run with no network and no bill. It
is refused in production.

## Vectors

| Variable | Default | Notes |
|---|---|---|
| `CHROMA_PERSIST_DIR` | `./var/chroma` | Embedded mode: a SQLite file plus index segments. **One writing process.** |
| `CHROMA_HOST` | unset | Set it to use a Chroma server instead, which is what lifts the single-writer limit. |
| `CHROMA_PORT` | `8000` | |
| `CHROMA_DISTANCE` | `cosine` | Recorded on the Chroma collection at creation and immutable after. |

## Retrieval and chunking

| Variable | Default | Notes |
|---|---|---|
| `CHUNK_SIZE_TOKENS` | `512` | Measured with tiktoken, not characters, because the token budget is what the model actually has. |
| `CHUNK_OVERLAP_TOKENS` | `64` | Must be smaller than the chunk size; validated. Overlap stops a sentence that straddles a boundary from being unretrievable. |
| `RETRIEVAL_TOP_K` | `5` | |

Changing either chunking setting affects **new** documents only. Existing
documents keep the chunking they were ingested with until they are
reprocessed (`POST .../reprocess`).

## Ingestion and jobs

| Variable | Default | Notes |
|---|---|---|
| `INGESTION_MODE` | `inline` | `inline` runs the job inside the upload request — one process to deploy, and the response reports the real outcome. `background` returns immediately and a worker claims it — what a large corpus needs, where a 200-page PDF must not hold an HTTP connection open. Both write the same durable job row first. |
| `INGESTION_MAX_ATTEMPTS` | `3` | Including the first. |
| `INGESTION_LEASE_SECONDS` | `600` | Must exceed the slowest realistic document. A lease that expires mid-job hands the same work to a second worker; the jobs are idempotent, so that is survivable, not free. |
| `INGESTION_POLL_SECONDS` | `2.0` | Worker idle poll interval. |
| `INGESTION_RECONCILE_BATCH` | `500` | Vector ids examined per page by the reconcile sweep. |

## Uploads

| Variable | Default | Notes |
|---|---|---|
| `MAX_UPLOAD_SIZE_MB` | `25` | |
| `UPLOAD_DIR` | `./var/uploads` | Must be shared between the API and the worker, or ingestion "succeeds" against a file the worker cannot read. |
| `ALLOWED_UPLOAD_TYPES` | PDF, TXT, Markdown, DOCX | An allow-list. The declared content type is checked *and* the bytes are sniffed; a `.docx` must actually contain `word/document.xml`. |

## Frontend

The Streamlit process reads **two** variables and no others. That is the
whole security story for it: it cannot leak what it was never given.

| Variable | Default | Notes |
|---|---|---|
| `RAG_API_URL` | `http://127.0.0.1:8000` | |
| `RAG_API_TIMEOUT` | `30` | Seconds, for non-streaming calls. |

If any backend secret (`DATABASE_URL`, `SECRET_KEY`, `OPENAI_API_KEY`, …) is
visible in the frontend's environment, the sidebar says so. It is reported
rather than fatal: the variable being *set* is not the leak — shipping it to
a browser would be, and this app never does — but whoever deployed it should
know their frontend container can read the database password.

---

## Keeping `.env.example` current

`.env.example` is the list of every variable this application reads. When you
add a setting to `app/core/config.py`, add it there too, with an empty value
and a one-line comment. A setting that exists only in the code is a setting
the next deployment will not know to set.

## A note on dependency pinning

`requirements.txt` pins direct dependencies to the versions this project is
developed and tested against; transitive ones are left to pip. A full lock
file (`pip-compile`, `uv lock`, Poetry) is the right answer for a team and
should be generated on the target platform — the pins here were exercised on
Windows with CPython 3.12.7 and have not been resolved on linux/amd64.
