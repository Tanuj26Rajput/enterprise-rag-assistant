# API guide

> Written for a client developer integrating with this service. Interactive
> reference at `/docs` when `ENVIRONMENT` is not `production`; this document
> covers the parts a schema cannot express — the error contract, the tenancy
> rules, and the shape of the event stream.

Base path: `/api/v1`. All request and response bodies are JSON except
document upload, which is `multipart/form-data`.

---

## Conventions

### Authentication

Every route except `/auth/register`, `/auth/login`, `/auth/refresh`,
`/health`, `/ready` and `/metrics` requires:

```
Authorization: Bearer <access_token>
```

Access tokens last 30 minutes by default; refresh tokens 7 days. The two
carry a `typ` claim and are not interchangeable — a refresh token presented
as an access token is rejected, which stops a long-lived credential from
being used as a short-lived one.

### Errors

Every failure, from any layer, has one shape:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "This action requires the admin role; you have member.",
    "details": null,
    "request_id": "9c1f2b7a4e8d4f1f9a2c3b5d7e8f0a1b"
  }
}
```

`request_id` also comes back in the `X-Request-ID` header and appears on
every log line and audit row for that call. Quote it in a bug report and the
whole request can be reconstructed.

| Status | `code` | Means |
|---|---|---|
| 400 | `bad_request` | Malformed request. |
| 401 | `authentication_failed` | Missing, expired or malformed token. |
| 403 | `permission_denied` | You are a member, but your role is insufficient — or the workspace/collection is deactivated. |
| 404 | `not_found` | The resource does not exist **or you are not a member of its namespace**. See below. |
| 409 | `conflict` | Slug already taken, duplicate upload, last owner removal. |
| 422 | `validation_failed` | Payload failed validation; `details` lists `{field, error}`. |
| 503 | `service_unavailable` | A provider or the vector store is unreachable. Retryable. |

**Why a non-member gets 404 and not 403.** 403 would confirm the id names a
real namespace, which is enough to enumerate other tenants one UUID at a
time. Once you *are* a member, an insufficient role returns 403 — you already
know the resource exists, and hiding it would only make a legitimate
permission problem impossible to diagnose.

### Request ids

Send your own `X-Request-ID` to correlate across services. It is honoured
only if it matches `^[A-Za-z0-9._-]{1,64}$`; anything else is replaced with a
generated one. The value reaches log files and audit rows, so an
unvalidated one would let a caller forge log lines with embedded newlines.

### Pagination

List endpoints take `?limit=` (default 20, max 100) and `?offset=`, and
return:

```json
{ "items": [ ... ], "total": 137, "limit": 20, "offset": 0 }
```

### Roles

| Role | Can |
|---|---|
| `owner` | Everything, including deleting the namespace and granting ownership. |
| `admin` | Manage members (not ownership), collections, documents, reconcile. |
| `member` | Create collections, upload, delete documents, search, chat. |
| `viewer` | Read and search only. |

---

## Authentication

### `POST /auth/register`

```json
{ "email": "ana@acme.com", "password": "correct-horse-battery-staple", "full_name": "Ana" }
```

→ `201` with the user. Passwords are hashed with Argon2id. The hash is not a
field on any response model, so it cannot be exposed by a serialization
mistake.

Minimum length is 12 by default and there are no composition rules —
requiring a digit and a symbol reliably produces `Password1!`, which is not
what makes a password strong.

### `POST /auth/login`

```json
{ "email": "ana@acme.com", "password": "..." }
```

→ `200`

```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

A wrong password and an unknown email both return the same 401 after the same
work — an early return on "no such user" is a timing oracle for which
addresses have accounts.

### `POST /auth/refresh` · `GET /auth/me`

`refresh` takes `{"refresh_token": "..."}` and returns a fresh pair. `me`
returns the authenticated user.

---

## Namespaces (tenants)

| Method | Path | Role |
|---|---|---|
| `POST` | `/namespaces` | any authenticated user (becomes owner) |
| `GET` | `/namespaces` | — |
| `GET` | `/namespaces/{id}` | member |
| `PATCH` | `/namespaces/{id}` | admin |
| `DELETE` | `/namespaces/{id}` | owner |

```json
{ "slug": "acme-legal", "name": "Acme Legal", "description": "Contracts and policy" }
```

The slug is permanent: it appears in URLs and in the name of the underlying
vector collection.

`PATCH` with `{"is_active": false}` **freezes writes** — uploads, new
collections and search are refused with 403 while reads keep working.
Reactivation goes through the role check rather than the write check, so
freezing a workspace cannot lock out the people who can unfreeze it.

### Members

| Method | Path | Role |
|---|---|---|
| `GET` | `/namespaces/{id}/members` | member |
| `POST` | `/namespaces/{id}/members` | admin |
| `PATCH` | `/namespaces/{id}/members/{member_id}` | admin |
| `DELETE` | `/namespaces/{id}/members/{member_id}` | admin |

`{"email": "sam@acme.com", "role": "member"}`. An admin cannot grant `owner`
— that would be an escalation past their own authority. The last owner cannot
be removed or demoted; a namespace with no owner is unmanageable.

Membership is returned **by user id, not by email**. Listing members would
otherwise let anyone in a workspace harvest the address of everyone else in
it.

---

## Collections

| Method | Path | Role |
|---|---|---|
| `POST` | `/namespaces/{ns}/collections` | member |
| `GET` | `/namespaces/{ns}/collections` | member |
| `GET` `PATCH` | `/namespaces/{ns}/collections/{id}` | member / member |
| `DELETE` | `/namespaces/{ns}/collections/{id}` | admin |
| `POST` | `/namespaces/{ns}/collections/{id}/reconcile` | admin |

```json
{ "slug": "handbook", "name": "Employee handbook", "description": "Current edition" }
```

The response carries `embedding_model`, `embedding_dimensions`,
`document_count` and `chunk_count`. The first two are **immutable** — they are
fixed when the collection is created. Mixing two embedding models in one index
does not error; it silently returns nonsense, because the distances stop being
comparable. To change models, create a new collection.

`PATCH {"is_active": false}` freezes the collection: no uploads, no search,
still listed and still reversible.

**`POST .../reconcile`** sweeps the vector index for ids with no live chunk
row behind them and deletes them, returning `{"vectors_removed": n}`. It is
safe to run at any time and never touches data a document still owns. It
exists because the database and the index are two stores that cannot be
updated in one transaction — see [trade-offs](trade-offs.md#two-stores-one-truth).

---

## Documents

### `POST /namespaces/{ns}/collections/{id}/documents`

`multipart/form-data`:

| Field | Required | Notes |
|---|---|---|
| `file` | yes | PDF, DOCX, TXT or Markdown. Max 25 MB by default. |
| `title` | no | Max 512 characters. Defaults to the filename. |

→ `201`

```json
{
  "id": "0f2a...",
  "filename": "handbook.pdf",
  "status": "ready",
  "mime_type": "application/pdf",
  "size_bytes": 184320,
  "page_count": 24,
  "chunk_count": 61,
  "token_count": 28455,
  "error_message": null
}
```

`status` is `ready` under `INGESTION_MODE=inline` and `queued` under
`background` — poll `GET .../documents/{id}` or `GET .../documents/{id}/jobs`.

**Validation is on the bytes, not the label.** The declared content type is
checked against the allow-list, and then the content is sniffed: a PDF must
start with `%PDF-`, a DOCX must be a zip containing `word/document.xml`. A
`.exe` renamed to `.pdf` is refused. Filenames are stripped of path
components and capped at 255 characters.

Re-uploading identical bytes to the same collection returns `409` with the
existing document's id rather than indexing a second copy.

`storage_uri` is never serialized to a client.

| Status | Means |
|---|---|
| `pending` | Stored, no job yet. A document stuck here is a lost upload. |
| `queued` | A job exists and is waiting. |
| `processing` | A worker holds a lease on it. |
| `ready` | Searchable. |
| `failed` | Retries exhausted, or a permanent failure. `error_message` says why. |
| `deleting` | Soft-deleted; the vector purge has not finished. Already unsearchable. |
| `archived` | Fully purged. |

### Other document routes

| Method | Path | Notes |
|---|---|---|
| `GET` | `/namespaces/{ns}/collections/{cid}/documents` | List; filter with `?status_filter=`. |
| `GET` | `/namespaces/{ns}/documents/{id}` | |
| `GET` | `/namespaces/{ns}/documents/{id}/chunks` | The exact text a citation can point at. |
| `GET` | `/namespaces/{ns}/documents/{id}/jobs` | Attempts, errors, backoff. The first thing to look at when a document will not ingest. |
| `POST` | `/namespaces/{ns}/documents/{id}/reprocess` | Re-extract and re-index. Idempotent. |
| `DELETE` | `/namespaces/{ns}/documents/{id}` | `204`. |

**What `DELETE` does.** It soft-deletes the row and returns immediately; the
vector and blob purge is a retryable background job. This is not a shortcut:
retrieval resolves every hit through the database, so the document is
unreachable the moment the soft delete commits — even if the index is
unreachable. Refusing the delete because the vector store is down would deny
the user something they are entitled to in order to tidy up a cache.

---

## Search

### `POST /namespaces/{ns}/collections/{id}/search`

```json
{ "query": "how much annual leave carries over?", "top_k": 5, "min_score": 0.2 }
```

→ `200`

```json
{
  "query": "how much annual leave carries over?",
  "hits": [
    {
      "chunk_id": "a1b2...",
      "document_id": "0f2a...",
      "document_title": "Employee handbook",
      "chunk_index": 12,
      "page_number": 4,
      "content": "An employee may carry over up to five days ...",
      "score": 0.81
    }
  ]
}
```

POST rather than GET: a query can be long, and keeping user questions out of
access logs and browser history is worth more than cacheability on a route
whose results change with every upload.

---

## Answering

### `POST /namespaces/{ns}/collections/{id}/answer` — one-shot

```json
{ "question": "How much annual leave carries over?", "top_k": 5 }
```

→ `200`

```json
{
  "question": "How much annual leave carries over?",
  "answer": "Up to five days may be carried into the next calendar year, with written manager approval [1].",
  "citations": [
    {
      "marker": 1,
      "document_id": "0f2a...",
      "document_name": "Employee handbook",
      "chunk_id": "a1b2...",
      "chunk_index": 12,
      "page_number": 4,
      "excerpt": "An employee may carry over up to five days ...",
      "score": 0.81
    }
  ],
  "has_sufficient_evidence": true,
  "grounded": true,
  "model": "anthropic/claude-sonnet-5.5",
  "retrieved_count": 5,
  "truncated": false,
  "usage": { "prompt_tokens": 1840, "completion_tokens": 64, "total_tokens": 1904 }
}
```

**Citations cannot be invented.** The model writes bracketed markers only. The
marker is resolved by this service against the passages it actually supplied;
one naming a passage that was not supplied is deleted from the prose as well
as ignored. Document names, page numbers and excerpts in the response come
from the database, never from the model.

**`has_sufficient_evidence: false`** means the model reported that the
passages do not support an answer. `answer` then carries a plain explanation
and `citations` is empty. This is a successful 200: "I don't know" is a
correct answer, and the alternative is a confident fabrication.

No `model` field is accepted in the request. The model is a deployment
setting; letting a client choose it turns an operational decision into an
attack surface and an unbounded bill.

### `POST /namespaces/{ns}/conversations/{id}/messages` — streaming (SSE)

```json
{ "question": "And for part-time staff?", "top_k": 5 }
```

→ `200`, `Content-Type: text/event-stream`. The thread supplies the prior
turns, bounded on two axes (message count *and* total characters, because
either bound alone is defeatable).

```
event: start
data: {"conversation_id":"5c2d...","model":"anthropic/claude-sonnet-5.5","retrieved_count":5}

event: token
data: {"delta":"Part-time staff accrue"}

event: token
data: {"delta":" leave pro rata"}

: keep-alive

event: done
data: {"answer":"Part-time staff accrue leave pro rata ...","citations":[...],
       "has_sufficient_evidence":true,"grounded":true,"model":"...",
       "retrieved_count":5,"finish_reason":"stop"}
```

| Event | When | Payload |
|---|---|---|
| `start` | Always, first. | `conversation_id`, `model`, `retrieved_count` |
| `token` | Zero or more. | `delta` — append it |
| `done` | On success. | The full answer, citations and usage |
| `error` | On failure. | `code`, `message` |
| `: keep-alive` | Periodically. | An SSE comment; ignore it |

**Client contract.**

1. **Expect either `done` or `error`, never both.** Once the stream has
   started the HTTP status is already 200, so an upstream failure can only
   be reported in-band.
2. **`done` repeats the whole answer.** A client that dropped a token, or
   that chose not to render incrementally, gets a canonical copy.
3. **Citations arrive only in `done`.** A marker is resolvable only once the
   text containing it is complete. Sending them earlier would mean guessing,
   and guessing is how citations get invented.
4. **Handle `: keep-alive`.** A line starting with `:` is a comment in the
   SSE grammar. It is sent because proxies cut idle connections and the first
   token can take seconds.
5. **Disconnecting stops the work.** The server checks and closes the
   upstream stream, so an abandoned request stops costing tokens. Whatever
   was generated is still saved to the thread.
6. **A failed turn is still a turn.** The question is persisted before the
   model is called and the partial answer is persisted with its error, so
   retrying costs nothing and the thread does not develop holes.

If your proxy buffers responses, the stream will arrive all at once at the
end. The service sends `X-Accel-Buffering: no` and
`Cache-Control: no-transform`, which covers nginx; other proxies may need
their own setting.

### Conversations

| Method | Path | Notes |
|---|---|---|
| `POST` | `/namespaces/{ns}/conversations` | `{"collection_id": "...", "title": null}` |
| `GET` | `/namespaces/{ns}/conversations` | Yours only |
| `GET` `PATCH` `DELETE` | `/namespaces/{ns}/conversations/{id}` | |
| `GET` | `/namespaces/{ns}/conversations/{id}/messages` | Ordered by `sequence` |

Conversations are **private to the user who created them**, not shared across
the namespace. A colleague with the same role cannot read your thread, and
`user_id` is deliberately absent from the response model because a thread is
only ever returned to its owner.

---

## Operations

| Path | Purpose | Auth |
|---|---|---|
| `GET /health` | Liveness. Always 200 while the process is up; consults nothing. | none |
| `GET /ready` | Readiness. 200 or 503 with per-dependency status. | none |
| `GET /metrics` | Prometheus exposition. | none — **keep it off the public listener** |

```json
// GET /ready, degraded
{
  "status": "not_ready",
  "version": "0.1.0",
  "dependencies": { "database": "unreachable", "vector_store": "ok" }
}
```

Wire `/health` to the liveness probe and `/ready` to the readiness probe,
not the other way round: using `/ready` for liveness turns a database
failover into a fleet-wide restart loop.

Metrics carry no identifiers — route *templates*, not paths; model names and
environment, not users or queries. That is what makes an unauthenticated
`/metrics` defensible, and it is still an internal endpoint.

| Metric | Type | Labels |
|---|---|---|
| `rag_http_requests_total` | counter | `method`, `route`, `status`, `status_class` |
| `rag_http_request_duration_seconds` | histogram | `method`, `route` |
| `rag_documents_processed_total` | counter | `outcome` |
| `rag_chunks_indexed_total` | counter | — |
| `rag_jobs_total` | counter | `kind`, `outcome` |
| `rag_job_duration_seconds` | histogram | `kind`, `outcome` |
| `rag_retrieval_duration_seconds` | histogram | — |
| `rag_retrieval_hits` | histogram | — |
| `rag_llm_requests_total` | counter | `model`, `outcome` |
| `rag_llm_duration_seconds` | histogram | `model` |
| `rag_llm_tokens_total` | counter | `model` |
| `rag_ready` | gauge | — |
| `rag_build_info` | gauge | `version`, `environment`, `embedding_model`, `llm_model` |

Counters are per-process and reset on restart, which is what Prometheus
expects. They are not a billing ledger — the durable record is the audit
table and the job rows.
