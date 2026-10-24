# API guide

> A short tour of the HTTP API, grouped by purpose. For the full interactive
> reference with every field, open `/docs` while the backend is running
> (available unless `ENVIRONMENT=production`).

Base path: `/api/v1`. All requests and responses are JSON, except document
upload which is `multipart/form-data`.

**Authentication:** send `Authorization: Bearer <access_token>` on every
route except register, login, refresh, and the health/metrics endpoints.

**Errors:** every failure has the same shape:

```json
{ "error": { "code": "permission_denied", "message": "...", "request_id": "..." } }
```

**Roles** (per namespace): `owner` > `admin` > `member` > `viewer`. Owners
can do anything in their namespace; viewers can only read and search.

---

## 1. Authentication

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/register` | Create an account |
| POST | `/auth/login` | Get an access token + refresh token |
| POST | `/auth/refresh` | Exchange a refresh token for a new access token |
| GET | `/auth/me` | Get the current user |

No authentication required for register/login/refresh. Passwords are hashed
with Argon2 and never returned in any response.

```bash
curl -X POST $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-strong-password"}'

curl -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-strong-password"}'
# → { "access_token": "...", "refresh_token": "...", "expires_in": 1800 }
```

---

## 2. Namespaces and collections

A **namespace** is a workspace (a tenant). A **collection** is a named group
of documents inside a namespace, with one embedding model.

| Method | Path | Role required |
|---|---|---|
| POST | `/namespaces` | any logged-in user (becomes owner) |
| GET | `/namespaces` | — (lists yours) |
| GET / PATCH / DELETE | `/namespaces/{id}` | member / admin / owner |
| POST / GET | `/namespaces/{id}/members` | admin |
| POST | `/namespaces/{ns}/collections` | member |
| GET / PATCH | `/namespaces/{ns}/collections/{id}` | member |
| DELETE | `/namespaces/{ns}/collections/{id}` | admin |

```bash
curl -X POST $API/namespaces -H "Authorization: Bearer $TOKEN" \
  -d '{"slug":"acme","name":"Acme"}'

curl -X POST $API/namespaces/$NS/collections -H "Authorization: Bearer $TOKEN" \
  -d '{"slug":"handbook","name":"Handbook"}'
```

A user who is not a member of a namespace gets **404**, not 403 — this stops
someone from confirming a namespace exists just by guessing its ID.

---

## 3. Document upload and lifecycle

| Method | Path | Notes |
|---|---|---|
| POST | `/namespaces/{ns}/collections/{id}/documents` | Upload a file |
| GET | `/namespaces/{ns}/collections/{id}/documents` | List documents |
| GET | `/namespaces/{ns}/documents/{id}` | One document's status |
| GET | `/namespaces/{ns}/documents/{id}/chunks` | The extracted, chunked text |
| POST | `/namespaces/{ns}/documents/{id}/reprocess` | Re-extract and re-index |
| DELETE | `/namespaces/{ns}/documents/{id}` | Delete (soft delete + background cleanup) |

Accepted file types: **PDF, DOCX, TXT, Markdown** (checked by file content,
not by extension). Max size is 25 MB by default.

```bash
curl -X POST $API/namespaces/$NS/collections/$COL/documents \
  -H "Authorization: Bearer $TOKEN" -F "file=@handbook.pdf"
```

A document's `status` moves through `pending → queued → processing → ready`
(or `failed` with an `error_message`). Re-uploading an identical file
returns `409 Conflict` instead of creating a duplicate.

---

## 4. Chat and streaming

| Method | Path | Notes |
|---|---|---|
| POST | `/namespaces/{ns}/collections/{id}/answer` | One-shot question → answer |
| POST | `/namespaces/{ns}/conversations` | Start a conversation |
| POST | `/namespaces/{ns}/conversations/{id}/messages` | Ask a question (streamed, SSE) |
| GET | `/namespaces/{ns}/conversations/{id}/messages` | Read the thread |

**One-shot answer:**

```bash
curl -X POST $API/namespaces/$NS/collections/$COL/answer \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"question":"How much annual leave carries over?"}'
```

```json
{
  "answer": "Up to five days may be carried into the next year [1].",
  "citations": [{"document_name": "Employee handbook", "page_number": 4, "excerpt": "..."}],
  "has_sufficient_evidence": true
}
```

If the retrieved documents don't support an answer, `has_sufficient_evidence`
is `false` and the answer plainly says so — it never makes something up.

**Streaming a conversation turn** sends Server-Sent Events:

```
event: start   → {"conversation_id": "...", "retrieved_count": 5}
event: token   → {"delta": "Up to five"}
event: done    → {"answer": "...", "citations": [...]}
event: error   → {"code": "...", "message": "..."}   (if something fails mid-stream)
```

Citations only appear in the final `done` event, once the whole answer (and
every `[n]` marker in it) can be resolved against the passages that were
actually retrieved.

---

## 5. Health checks

| Path | Purpose | Auth |
|---|---|---|
| `GET /health` | Liveness — is the process up? | none |
| `GET /ready` | Readiness — can it reach PostgreSQL and ChromaDB? | none |
| `GET /metrics` | Prometheus metrics | none (keep this internal) |

Use `/health` for your liveness probe and `/ready` for readiness — using
`/ready` for liveness would restart the whole fleet on a database blip.

---

See [docs/architecture.md](architecture.md) for how these pieces fit
together, and [docs/trade-offs.md](trade-offs.md) for why key decisions were
made this way.
