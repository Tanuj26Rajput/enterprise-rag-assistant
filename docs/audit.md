# Security and correctness audit

> Written for a reviewer. It records what was examined, what was found, what
> was fixed, and — at least as importantly — what is still open and what was
> never verified. A review that reports only its successes is not a review.

**Date:** 2026-10-22 · **Scope:** the whole repository, against the
Enterprise Multi-Document RAG Assistant requirements.

---

## Verification performed

Everything below was actually run, and the output is what is quoted.

| Check | Command | Result |
|---|---|---|
| Test suite | `pytest -q` | **472 passed**, 0 failed |
| Lint | `ruff check .` | All checks passed |
| Format | `ruff format --check .` | 123 files already formatted |
| Migration 0003 | `alembic upgrade 0002:head --sql` | Rendered correctly offline |
| Compose schema | `docker compose config --quiet` | Valid |
| Entrypoint | `sh -n docker/entrypoint.sh` | Valid |

### What was *not* verified

Stated plainly, because the alternative is a reader assuming otherwise.

- **The Docker image was never built.** The Docker daemon was not running in
  this environment (`open //./pipe/dockerDesktopLinuxEngine: The system
  cannot find the file specified`). The Dockerfile and compose file are
  syntactically valid and the compose schema validates; **no container has
  been built, started, or health-checked.** Build it before trusting it.
- **No migration was applied to a live database.** `0002_ingestion_jobs` and
  `0003_partial_slug_uniqueness` are both **pending** against the configured
  Supabase instance. They were rendered to SQL offline and reviewed, not run.
  Apply them in a maintenance window — `0002` adds enum values and `0003`
  rebuilds two unique indexes.
- **No test runs against live PostgreSQL.** The suite fakes the SQL layer.
  Constraints, cascades and the `SKIP LOCKED` claim are therefore asserted
  against the model metadata and against fakes, not against a server. See
  [open gaps](#known-gaps).
- **No load testing.** Every performance statement in these documents is
  reasoning about the design, not a measurement.
- **The LLM was never called for real.** The chat provider is scripted
  throughout, deliberately.
- **The Mermaid diagrams were not rendered.** No renderer was available here,
  so they have been reviewed as source but not seen as pictures. If one fails
  to draw on GitHub, it is a syntax error in that block, not a claim about the
  system.

---

## Defects found and fixed

Each was confirmed before being fixed — reproduced, or traced to a specific
line — and each now has a regression test in `tests/test_hardening.py`.

### 1. `is_active` was a field nothing read — **high**

`Namespace.is_active` and `Collection.is_active` were settable through
`PATCH`, returned in responses, and enforced nowhere. An administrator could
freeze a workspace, watch the API confirm it, and have every upload and
every search keep working.

A flag a user can set that changes nothing is worse than no flag: it reports
a state the system is not in.

**Fixed.** `NamespaceContext.require_write()` now refuses a deactivated
namespace, and a new module-level `require_active_collection()` is called
from `DocumentService.upload` and `RetrievalService.search`.

The freeze is a *write* freeze, not a blackout. Reads keep working, because
locking members out of their own documents turns an administrative pause
into data loss as far as they are concerned. Reactivation goes through
`require()` with a role minimum rather than through `require_write()`, so
freezing a workspace cannot lock out the people who can unfreeze it.

### 2. Client-controlled `X-Request-ID` reached logs and audit rows — **medium**

The header was taken verbatim and written to every log line and audit row
for the request. Two consequences:

- **Log injection.** A value containing `\r\n` splits one log record into
  two, letting a caller forge entries — including entries attributed to
  another request.
- **Unbounded growth.** An arbitrarily long value propagated through every
  log sink in the path and into a sized database column.

**Fixed.** Honoured only when it matches `^[A-Za-z0-9._-]{1,64}$`, replaced
with a generated id otherwise. Client correlation still works for ids that
look like ids.

### 3. Unbounded filename reached a sized column — **medium**

Reproduced before fixing: a 604-character filename was accepted and sent to
a `String(512)` column, which fails at the INSERT — a 500 for what is plainly
a bad request. Re-verified after the fix.

**Fixed.** `MAX_FILENAME_LENGTH = 255` enforced in
`app/ingestion/validation.py`, after path components are stripped (so the
limit cannot be evaded by a long directory prefix). 255 is what every common
filesystem imposes anyway, so nothing legitimate is refused.

### 4. Unbounded upload `title` — **low**

Same shape, different field. **Fixed:** `Form(max_length=512)`.

### 5. Stored blobs were never deleted — **medium**

Deleting a document purged its vectors and closed its row; the uploaded
original stayed on disk forever. On a long-lived deployment that is an
unbounded disk leak and — worse — a retained copy of data a user asked to
have deleted.

**Fixed.** `DocumentLifecycleService` now takes the storage adapter and
deletes the blob in `_run_delete`, wired through `app/api/deps.py` and
`app/worker.py`. The delete job is retryable, so the purge is idempotent:
a blob that is already gone is not an error.

### 6. Prompt injection from uploaded documents — **mitigated, not fixed**

Uploaded text reaches the model, so a document can contain instructions
addressed to it.

**Mitigated.** The system prompt now states that passages are quoted data and
never instructions, and `build_user_prompt` fences them with explicit markers
with the question outside the fence.

**This is labelled a mitigation in the code and in the tests, because that is
what it is.** Everything inside the fence is still text a model reads. What
actually bounds the damage is structural, and was already true: the model
sees only passages this caller was authorized to retrieve, it has no tools,
and its citation markers are resolved by this application against the
retrieved set — so an injected instruction cannot invent a source or attribute
a quotation to a document that does not contain it. Residual risk is in
[trade-offs.md](trade-offs.md#prompt-injection-from-uploaded-documents).

### 7. Soft-deleted slugs were reserved forever — **medium**

`namespaces.slug` and `uq_collections_namespace_id_slug` were plain unique
constraints over *every* row, including soft-deleted ones. Deleting the
`handbook` collection and creating it again returned a 409 naming a row the
user can no longer see, and no amount of deleting fixed it.

**Fixed.** Both are now partial unique indexes scoped to
`deleted_at IS NULL`, with migration `0003_partial_slug_uniqueness`. The
downgrade is deliberately explicit about being lossy: restoring a global
constraint requires removing the soft-deleted rows that collide, and it does
that rather than appearing to succeed.

### 8. Three bugs in the new observability code — **found by its own tests**

Worth recording because they are the reason the tests exist.

- **Histogram buckets were double-counted.** `observe()` already stored
  cumulative counts; `render()` accumulated them again, so `le="0.25"`
  reported 5 where the answer was 2 — silently wrong percentiles.
- **Metric and log route labels lacked the API prefix.** This FastAPI version
  keeps an included router nested rather than flattening its routes, so
  `route.path` was `/namespaces/{namespace_id}` — a template matching no URL
  this service serves, and one that would collide with any future unversioned
  route of the same shape.
- **`rag_build_info` was set in `lifespan`,** which does not run under an
  in-process ASGI client, so it was absent exactly where it was being
  checked. Moved to `create_app`, where it belongs: it describes the
  configuration the process was constructed with.

---

## Examined and found correct

Recorded so a future reviewer knows these were looked at rather than skipped.

| Area | Finding |
|---|---|
| **Cross-tenant access** | Every namespace-scoped service call starts at `NamespaceAuthorizer`. Repository methods take `namespace_id` as a required positional argument. The two `get_any()` escapes are jobs-only, named to be conspicuous, and reachable only after the tenant has been resolved from the job row. Vector queries carry a store-side `$and` tenant filter **and** every hit is re-resolved through a tenant-scoped query, so a leaked vector cannot become a visible citation. |
| **Namespace enumeration** | Non-membership returns 404, not 403. Insufficient role within a namespace returns 403. |
| **Password handling** | Argon2id via pwdlib. `hashed_password` is on no response model, so it cannot be leaked by a serialization slip. Login does the same work for an unknown email as for a wrong password — no timing oracle. |
| **JWT** | Explicit `algorithms=[...]` on decode (no `alg: none` or algorithm-confusion). A `typ` claim prevents a refresh token being presented as an access token. |
| **Upload validation** | Magic-byte sniffing, not the declared content type. A DOCX must contain `word/document.xml`. Path components stripped from filenames. Size enforced before the bytes are read into a model. |
| **Secret exposure** | `SecretStr` throughout; `storage_uri` never serialized; the frontend has four non-credential settings and a test that asserts it. |
| **Privilege escalation** | An admin cannot grant `owner`. The last owner cannot be removed or demoted. |
| **Conversation isolation** | Threads are private to their creator, not shared by namespace. `user_id` is deliberately absent from the response model. |
| **Error handling** | One error envelope for every failure. Pydantic validation errors are reduced to `{field, error}` — the raw ones echo submitted values, which for a login payload means the password. |
| **SSE failure paths** | Disconnect, upstream failure and refusal each have a path and a test. A failed turn is persisted with its error, so the thread does not develop holes and retrying is free. |
| **Job idempotency** | Derived vector ids, wholesale chunk replacement, delta counters, partial unique index on active jobs. Running any job twice converges. |
| **Startup DDL** | There is none. The schema belongs to Alembic. |

---

## Known gaps

Open, with the honest reason each is open.

### 1. Tenant isolation is enforced in application code, not by the database

A repository method written without its namespace predicate is a
cross-tenant leak and PostgreSQL will not stop it. The mitigations
(required positional arguments, composite foreign keys, re-resolution of
every vector hit) narrow it; they do not close it.

**The fix is row-level security** with the namespace in a session variable,
which makes a forgotten predicate fail closed. Not implemented: it interacts
with connection pooling in ways that need care — a transaction pooler hands a
connection to another client between transactions, so the session variable
must be set per transaction, not per connection.

### 2. No test runs against live PostgreSQL

The fakes can drift from the real SQLAlchemy repositories, and the things the
fakes cannot model are precisely the things most worth testing: constraint
violations, cascade behaviour, the `FOR UPDATE SKIP LOCKED` claim under
contention, and the partial indexes added in `0003`.

`tests/test_schema.py` pins the table set and the new tests assert index
*definitions*, which is a partial guard. The real answer is testcontainers in
CI.

### 3. Rate limiting

There is none. `/auth/login` can be attempted as fast as a client can post,
and `/answer` can be called in a loop against a metered provider. Argon2id
makes credential stuffing expensive per attempt, and
`MAX_UPLOAD_SIZE_MB`/`ANSWER_EXCERPT_CHARS`/`LLM_MAX_TOKENS` bound individual
requests — but nothing bounds *rate*.

Deploy behind something that does, and treat a per-tenant token budget as the
next piece of work.

### 4. Embedded ChromaDB is single-writer

The full argument is in
[trade-offs.md](trade-offs.md#chromadb-persistence-and-its-limits). In short:
SQLite-backed, one writing process, local filesystem only, no replication, no
authentication. `WEB_CONCURRENCY=1` and one worker replica are the defaults
because of it. `CHROMA_HOST` is the first step out.

### 5. No refresh-token revocation

Tokens are stateless. Signing out discards the client's copy; the token
remains valid until it expires. A stolen refresh token is usable for up to
`REFRESH_TOKEN_EXPIRE_DAYS`. The blunt instrument is rotating `SECRET_KEY`,
which invalidates everyone's.

A `token_version` column on `users`, included as a claim and checked on
refresh, would fix it for a modest cost.

### 6. Audit log retention and access

`audit_logs` grows without bound and has no retention policy, no partitioning
and no API to read it. It records what it should — actor, action, outcome, IP,
request id — but only someone with database access can look at it.

### 7. Dependencies are pinned, not locked

`requirements.txt` pins direct dependencies; transitive ones resolve freely,
and the pins were exercised on Windows/CPython 3.12.7, not on linux/amd64. A
lock file generated on the target platform is the proper fix. There is also
no dependency vulnerability scanning in this repository.

### 8. `/metrics` is unauthenticated

Deliberate and bounded — it carries route templates, model names and counts,
with no identifiers, payloads or query text — but it is an internal endpoint
served on the same listener as everything else. Keep it off the public
listener at the ingress.

### 9. Single-node operational assumptions

Metrics are per-process (so counts are per-replica, which is what Prometheus
expects, but means `WEB_CONCURRENCY=1`). Streamlit session state is
per-process, so scaling the frontend needs sticky sessions.

---

## Things the author should act on

1. **Rotate the OpenAI key in `.env`.** It was exposed in this working
   session. Rotating it is the only remedy; nothing else in this report
   substitutes for it.
2. **Fix `EMBEDDING_MODEL` in `.env`.** It is currently
   `openai/text-embedding-3-small` with `EMBEDDING_PROVIDER=openai`, which
   fails validation at start-up. Use `text-embedding-3-small`.
3. **Apply migrations `0002` and `0003`** to the Supabase instance when you
   are ready. Both are pending and neither was run from here.
4. **Build the Docker image** and bring the compose stack up once. It has not
   been executed.
