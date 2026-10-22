# Troubleshooting

> Written for whoever is looking at a broken instance. Organised by what you
> *see*, not by what is wrong — you do not yet know what is wrong.

**Before anything else:** get the request id. It is in the `X-Request-ID`
response header, in the `error.request_id` field of every error body, and on
every log line and audit row for that call.

```bash
# Every line from one request
grep '"request_id":"9c1f2b7a..."' app.log | jq .
```

---

## It will not start

### `EMBEDDING_MODEL='openai/text-embedding-3-small' uses the vendor-prefixed form…`

`EMBEDDING_PROVIDER=openai` calls OpenAI directly, where the model is named
`text-embedding-3-small` with no vendor prefix. The prefixed form is what
routers such as OpenRouter expect.

Either `EMBEDDING_MODEL=text-embedding-3-small`, or
`EMBEDDING_PROVIDER=openrouter` **plus** an explicit `EMBEDDING_DIMENSIONS`.

This is checked at start-up on purpose: the alternative is discovering it
when a collection has already been created at the wrong width.

### `SECRET_KEY must be set to a strong value in production`

`ENVIRONMENT=production` refuses the placeholder and anything under 32
characters.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Also refused in production: `DEBUG=true`, `CORS_ORIGINS=*`,
`EMBEDDING_PROVIDER=hash`.

### `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`

Windows only. Use `python run.py`, which supplies a selector loop. `uvicorn
app.main:app` works directly on Linux.

### `ValidationError: DATABASE_URL Field required`

No `.env`, or it is not in the working directory. `cp .env.example .env` and
fill it in. Note that an *empty* `KEY=` is treated as unset, so a blank
`DATABASE_URL` produces this same message.

### `relation "users" does not exist`

Migrations have not been run. The application never creates tables —
several replicas booting together would race to issue DDL and the running
schema would drift from the migration history.

```bash
alembic upgrade head
alembic current          # what is applied
alembic heads            # what should be
```

---

## Uploads

### 422 `Unsupported file type`

Only PDF, DOCX, TXT and Markdown. The check is on the **bytes**, not the
extension or the declared content type: a PDF must start with `%PDF-`, and a
DOCX must be a zip containing `word/document.xml`. A `.doc` (the old binary
format) renamed to `.docx` fails here, correctly — convert it first.

### 422 `The filename is too long`

Capped at 255 characters after path components are stripped.

### 409 `An identical document already exists in this collection`

Same SHA-256 in the same collection. `details.document_id` is the existing
one. Upload it to a different collection, or delete the original first.

### 413, or the upload dies without a response

Over `MAX_UPLOAD_SIZE_MB` (default 25). If you raise it, raise your reverse
proxy's limit too — nginx's `client_max_body_size` defaults to 1 MB and will
reject the request before this service sees it.

### Deleting a `failed` document returns 500

Fixed by migration `0004_deletable_failed_documents`; if you see it, that
revision has not been applied. Run `alembic upgrade head`.

The cause was a CHECK constraint written as a biconditional — a failed
document must carry an error message, and nothing else may. Deletion moves a
document to `deleting` and then `archived` while keeping the message, so for
a previously-failed document the `UPDATE` violated the constraint:

```
psycopg.errors.CheckViolation: new row for relation "documents" violates
check constraint "ck_documents_error_message_matches_status"
```

The symptom is a generic 500 with a request id; the cause is only in the
logs. Confirm which rule your database has:

```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint
 WHERE conname = 'ck_documents_error_message_matches_status';
```

A `CASE status WHEN 'failed' ... WHEN 'deleting' THEN true ...` is the fixed
form. A bare `(status = 'failed') = (error_message IS NOT NULL)` is the old
one, and every failed document in that database is undeletable.

### 403 `This workspace is deactivated` / `The collection … is deactivated`

Someone set `is_active: false`. Reactivate with
`PATCH /namespaces/{id}` or `PATCH /namespaces/{ns}/collections/{id}` and
`{"is_active": true}`. Reads keep working while frozen; writes and search do
not.

---

## Documents that will not become `ready`

**Look at the job history first. It has the actual error.**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/namespaces/$NS/documents/$DOC/jobs" | jq '.items[]'
```

| `status` | Means | Do |
|---|---|---|
| `queued` with `run_after` in the future | Waiting out a backoff. | Wait. `last_error` says why it failed. |
| `queued` with `run_after` in the past | Nothing is consuming the queue. | See below. |
| `running` with an expired `lease_expires_at` | The worker died. | Restart a worker; start-up requeues abandoned jobs. |
| `failed` | Retries exhausted, or a permanent failure. | Read `last_error`, fix the cause, `POST .../reprocess`. |

### Stuck in `queued` and nothing is moving

`INGESTION_MODE=background` and no worker is running. Either start one
(`python -m app.worker`, or the `worker` compose service), or set
`INGESTION_MODE=inline` for a single-process deployment.

```bash
docker compose ps worker
docker compose logs worker --tail=50
```

### Stuck in `pending` with no job row at all

This is a lost upload — the two statuses are distinguished precisely so you
can tell. It means the job INSERT did not commit. `POST .../reprocess`
enqueues a fresh one.

### `failed` with `error_message` about extraction

A scanned PDF with no text layer extracts to nothing. There is no OCR in this
pipeline. Run the file through OCR (`ocrmypdf`) and re-upload.

### `status: ready` but `chunk_count: 0`

The file parsed but contained no extractable text — again, usually a scanned
PDF or an empty document.

---

## Search returns nothing

Work down this list; it is ordered by how often each one is the cause.

1. **Is the document `ready`?** `queued`, `processing` and `failed`
   documents are not searchable.
2. **Is `chunk_count` greater than zero?**
3. **Is `min_score` too high?** Drop it, or omit it. Cosine similarity on
   short queries is often lower than people expect.
4. **Is the collection deactivated?** It returns 403, not empty — but check.
5. **Was the collection built with a different embedding model?** The
   collection's `embedding_model` is in its response body. If it does not
   match the current `EMBEDDING_MODEL`, the vectors are not comparable. The
   fix is a new collection; the model is immutable by design.
6. **Did `EMBEDDING_PROVIDER` change from `hash` to a real provider (or back)?**
   Same problem. Hash embeddings are syntactic and share no geometry with a
   real model's.
7. **Is the index ahead of the database?** Run
   `POST .../collections/{id}/reconcile`. The log line *"Dropped N of M
   vector hit(s) with no live chunk"* is the signature.

---

## Answering

### 503 `Answer generation is temporarily unavailable`

The chat provider is unreachable, rate-limiting, or the key is wrong.
Retries have already been attempted (`LLM_MAX_ATTEMPTS`).

```bash
curl -s "$API/metrics" | grep rag_llm_requests_total
```

The `outcome` label distinguishes a timeout from a rate limit from an auth
failure. Check that `LLM_PROVIDER` matches the key you set — an
`OPENAI_API_KEY` is never sent to OpenRouter, deliberately.

### 503 `The answer service is not correctly configured`

A distinct code — `upstream_misconfigured` — because retrying will not help.
No key is set for the configured provider, or `LLM_MODEL` is a bare name
while `LLM_PROVIDER=openrouter` (OpenRouter namespaces every model by vendor,
so `gpt-4o-mini` is a 404 and `openai/gpt-4o-mini` is not).

The provider's own wording is in the logs, never in the response — upstream
errors routinely quote the request back, which here would mean echoing the
prompt and the retrieved passages to the client. Join them on the request id:

```bash
docker logs <api-container> | grep <request_id-from-the-error-body>
```

Everything except answering keeps working in this state, deliberately:
uploads, indexing, search, and the whole conversation surface do not need a
model.

### The answer is empty, but the request succeeded

`finish_reason: length` with no content. The model is a **reasoning** model
and spent the entire `LLM_MAX_TOKENS` budget on hidden reasoning tokens
before writing anything. OpenRouter counts those against `max_tokens`, and
the client coerces a null content to `""` — so this surfaces as a blank
answer rather than an error, which looks like a broken application.

Confirm it by reading the usage block:

```bash
curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$LLM_MODEL"'","max_tokens":1024,
       "messages":[{"role":"user","content":"Reply with the single word: ok"}]}' \
  | jq '{finish: .choices[0].finish_reason,
         content: .choices[0].message.content,
         reasoning_tokens: .usage.completion_tokens_details.reasoning_tokens}'
```

A non-zero `reasoning_tokens` is the diagnosis. Either raise
`LLM_MAX_TOKENS` well above the reasoning overhead, or pick a model that
does not reason — measured, `stepfun/step-3.7-flash` and
`anthropic/claude-sonnet-5.5` report `reasoning_tokens: 0` and answer
correctly at the default 1024, while `stepfun/step-3.5-flash` does not.

### `429` / `openrouter_admission_control` — "could not verify available credits"

An account-level check, not a model one. `provider_name: None` in the error
metadata means the request never reached a model, so **switching models does
not help** — the same limit applies to all of them, free tiers included.

The service now retries this automatically when it happens before the first
token, honouring the provider's `Retry-After`. Seeing it reach the UI means
either every attempt was rejected (`LLM_MAX_ATTEMPTS`, default 3) or the
failure arrived *after* streaming had begun, which is deliberately not
retried — a retry there would splice two different answers together.

If it is persistent rather than occasional:

- Slow the request rate for the account; this limiter is per-account, so
  parallel questions from several users compound.
- Add credits. The admission-control check is specifically about verifying
  available credit, and it fires more readily on a zero-balance free tier.
- Raise `LLM_MAX_ATTEMPTS`, which costs latency on a genuinely dead provider.

### Every answer is `has_sufficient_evidence: false`

The model is being honest and retrieval is bringing back nothing useful.
Work the "Search returns nothing" list above — this is that symptom, one
layer up.

### The answer has no citations

Either the model refused (`has_sufficient_evidence: false`), or it wrote
markers naming passages that were not supplied and they were stripped. The
second shows up as cited-looking prose with no citation list, and it means
the model is not following the format — try a more capable `LLM_MODEL`.

### The stream arrives all at once at the end

A proxy is buffering. The service sends `X-Accel-Buffering: no` and
`Cache-Control: no-transform`, which covers nginx; other proxies need their
own setting. If you added one yourself, start there.

### The stream stops after ~60 seconds with no `done`

A proxy idle timeout. Keep-alive comments (`: keep-alive`) are sent to
prevent exactly this, so check your proxy is not stripping comment lines, and
raise its read timeout above `LLM_REQUEST_TIMEOUT`.

---

## Database

### `database is locked` from Chroma

Two processes are writing the same `CHROMA_PERSIST_DIR`. Embedded Chroma is
SQLite-backed and supports **one** writer.

Set `WEB_CONCURRENCY=1`, run one worker replica, and for anything larger move
Chroma to its own service with `CHROMA_HOST`. Full explanation in
[trade-offs.md](trade-offs.md#chromadb-persistence-and-its-limits).

### 500 and `MissingGreenlet: greenlet_spawn has not been called`

Raised while serializing a row that was just updated, naming `updated_at` or
another server-computed column:

```
Error extracting attribute: MissingGreenlet: greenlet_spawn has not been
called; can't call await_() here.
```

A server-side default or `onupdate` leaves the attribute expired after an
`UPDATE`, and pydantic reads attributes synchronously, where the deferred
reload has no greenlet to run in. `Base` sets
`__mapper_args__ = {"eager_defaults": True}` to fetch those values through
`RETURNING` in the same statement, which is what prevents it.

If this reappears, a model has almost certainly declared its own
`__mapper_args__`, which *replaces* the base mapping rather than merging with
it — so the setting is lost for that one table. Merge it back in:

```python
__mapper_args__ = {**Base.__mapper_args__, "polymorphic_identity": "..."}
```

`tests/test_live_sql_regressions.py` guards this. Note that
`expire_on_commit=False` does not help here: the expiry is set by the flush
that issued the `UPDATE`, not by the commit.

### `prepared statement "_pg3_0" already exists`

The runtime connection is going through a transaction-mode pooler on a port
the application does not recognise as one. Server-side prepared statements
cannot survive a connection being handed to another client between
transactions.

Port **6543** is detected automatically (Supavisor/PgBouncer transaction
mode) and switches to `NullPool` with prepared statements off. If your pooler
is on another port, either move it to 6543 or disable prepared statements at
the pooler.

### `QueuePool limit of size 10 overflow 5 reached`

More concurrent requests than connections. Either raise `DB_POOL_SIZE`, or —
more likely correct — find the slow query holding connections open.
`DB_STATEMENT_TIMEOUT_MS` (default 30s) bounds the worst case.

### Alembic hangs on `upgrade head`

DDL through a transaction pooler. Set `DATABASE_DIRECT_URL` to the direct
endpoint (port 5432); Alembic prefers it whenever it is set.

---

## Frontend

### `ModuleNotFoundError: No module named 'frontend'`

Run it as `streamlit run frontend/app.py` from the repository root.
`streamlit run` puts the *script's* directory on `sys.path`, not the repo
root — `frontend/app.py` bootstraps `sys.path` for exactly this reason, so
if you see this, something is importing before that bootstrap.

### The sidebar warns about leaked secrets

The frontend process can see a backend-only variable (`DATABASE_URL`,
`SECRET_KEY`, an API key…). Usually one shared `.env` given to every
container. Nothing is being shipped to the browser — but that container can
read your database password, which is one compromise away from mattering.
Give the frontend only `RAG_API_URL`; see the `frontend` service in
`docker-compose.yml`.

### Everything 401s after a while

The access token expired and the refresh failed. The client refreshes once
and retries once. Sign out and back in; if it recurs, check that
`SECRET_KEY` is identical across every API replica — a rolling deploy with
two different keys invalidates tokens at random.

---

## Operations

### The orchestrator keeps restarting healthy containers

`/ready` is probably wired to the liveness probe. `/ready` returns 503 when a
*dependency* is down, and a liveness failure means restart — so a database
failover restarts the whole fleet, and the restarted containers cannot reach
the database either.

- liveness → `/health` (never consults a dependency)
- readiness → `/ready`

### `/ready` says `vector_store: unreachable`

The persist directory is missing, unwritable, or on a volume that failed to
mount. In Docker, check the `rag-data` volume is attached to both `api` and
`worker` — and that it is the *same* volume, or the worker writes vectors the
API cannot find.

### `/metrics` is huge, or the scraper is struggling

A high-cardinality label has crept in. Route labels must be path
*templates*; nothing user-supplied may become a label value. Check for a new
`metrics.counter(...)` call with an id in it.

```bash
curl -s "$API/metrics" | cut -d'{' -f1 | sort | uniq -c | sort -rn | head
```

### Logs are unreadable walls of JSON

`LOG_JSON=false` gives console format. Leave it on in containers, where a
collector parses them.

---

## Diagnostic quick reference

```bash
# Health, readiness, metrics
curl -s "$API/health" | jq .
curl -s "$API/ready"  | jq .
curl -s "$API/metrics" | grep -E 'rag_(jobs|documents|llm)'

# Migration state
alembic current && alembic heads

# Stuck jobs, straight from the database
psql "$DATABASE_URL" -c "
  SELECT kind, status, attempts, run_after, lease_owner, left(last_error, 80)
  FROM ingestion_jobs
  WHERE status IN ('queued','running')
  ORDER BY run_after;"

# Orphaned vectors in one collection
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/namespaces/$NS/collections/$COL/reconcile" | jq .

# Does the suite still pass?
pytest -q && ruff check . && ruff format --check .
```
