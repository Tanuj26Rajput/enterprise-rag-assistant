# Security and correctness audit

> Written for a reviewer. It records what was examined, what was found, what
> was fixed, and — at least as importantly — what is still open and what was
> never verified. A review that reports only its successes is not a review.

**Date:** 2026-10-22, extended 2026-10-23 · **Scope:** the whole repository,
against the Enterprise Multi-Document RAG Assistant requirements.

---

## Verification performed

Everything below was actually run, and the output is what is quoted.

| Check | Command | Result |
|---|---|---|
| Test suite | `pytest -q` | **494 passed**, 0 failed |
| Lint | `ruff check .` | All checks passed |
| Format | `ruff format --check .` | 125 files already formatted |
| Compose schema | `docker compose config --quiet` | Valid |
| Entrypoint | `sh -n docker/entrypoint.sh` | Valid |

### Verified in containers (2026-10-23)

The Docker daemon became available, so the claims this document previously
marked unverified were tested for real. **Doing so found four defects the
484-test suite passes straight over** — see
[defects found by running it](#defects-found-by-running-it-2026-10-23).

Against image `enterprise-rag:test` on a throwaway `postgres:17-alpine`
container, with `EMBEDDING_PROVIDER=hash` and **no** LLM key:

| Check | Result |
|---|---|
| Image build | Succeeds, 1.72 GB |
| `migrate` role | `0001 → 0002 → 0003` applied to a real database |
| Migration 0003 round trip | `downgrade 0002` then `upgrade head` — both clean |
| Resulting DDL | Both slugs are *partial* unique indexes; the old constraints are gone |
| Slug reuse | A second live `handbook` is refused; reuse after soft delete succeeds |
| `api` role | Serves; `/health` 200, `/ready` 200 with `database: ok, vector_store: ok` |
| `worker` role | Claims a job, indexes it, document reaches `ready` with 1 chunk |
| `frontend` role | Streamlit healthy; its environment carries **no** credentials |
| Container healthchecks | `frontend` reports `healthy`, `worker` reports plain `Up` (see defect 13) |
| Retrieval | `POST /search` returns the indexed chunk |
| Cross-tenant access | Another tenant gets **404** on read, search and delete; **401** unauthenticated; owner **200** |
| Metrics cardinality | Only route *templates* appear — no UUID reaches a label |
| `/answer` with no key | **503 `upstream_misconfigured`**, not 500 |

### What is *still* not verified

Stated plainly, because the alternative is a reader assuming otherwise.

- **The schema of the project's Supabase instance was not inspected after
  migrating.** `0002` and `0003` were applied there by the author on
  2026-10-23 and `alembic current` reports `0003_partial_slug_uniqueness
  (head)`, so the chain ran. What was *not* done from here is reading back
  the resulting objects — confirming the two slug indexes are partial and
  the old constraints are gone. That is one query, and it is worth running
  (see [verifying it yourself](#verifying-the-live-schema)): Alembic reports
  the revision it recorded, not the DDL it produced.
- **No test runs against live PostgreSQL.** The suite still fakes the SQL
  layer. This is the gap that hid all four defects below, and it is the single
  most valuable thing left to close. The container run was manual, not a test.
- **`docker compose up` was never run.** Deliberately: `DATABASE_URL` in the
  ambient `.env` resolves to a live Supabase instance, so the `migrate`
  service would have applied pending migrations to production. The individual
  roles were run by hand against a throwaway database instead.
- **No load testing.** Every performance statement in these documents is
  reasoning about the design, not a measurement.
- **Retrieval quality was never measured.** The container run used `hash`
  embeddings, which are random projections carrying no semantics. It proves
  the pipeline moves vectors end to end; it says nothing about relevance.
- **The streaming path was not exercised in a container.** `/answer` was
  tested only to the point of its 503.
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

## Defects found by running it (2026-10-23)

Five of the six below (9–12 and 14) are kept together because of what they
have in common: **every one passed the full test suite, and every one
returned HTTP 500 the first time real code touched a real PostgreSQL.** Four
surfaced within minutes of pointing the image at a database; the fifth was
reported by a user clicking a button, and reproduced in minutes once there
was a real server to reproduce it against. They are the strongest available
evidence for [gap 2](#2-no-test-runs-against-live-postgresql--the-most-expensive-gap-here),
and the reason that gap is ranked first among the open ones.

Defect 13 is the odd one out — a container healthcheck, found by reading
`docker ps` — and is recorded here because it has the same cause: a thing
that was never run.

Each fix has a regression guard in `tests/test_live_sql_regressions.py`. None
of those guards can reproduce the original failure — that needs live
PostgreSQL — so each instead asserts the *property* the fix established, and
each was confirmed to fail when the fix is reverted. That is a weaker
guarantee than a real test, and it is stated plainly here because the
distinction matters: these guards catch a revert, never a new instance of the
same class.

### 9. Every update-then-return endpoint returned 500 — **critical**

`TimestampMixin.updated_at` carries a server-side `onupdate=func.now()`.
SQLAlchemy cannot predict what the database clock will produce, so after any
`UPDATE` it marks the attribute expired and defers a refetch to the next
access.

Under asyncio that deferral is fatal rather than merely slow. Responses are
built with `DocumentRead.model_validate(document)`, and pydantic reads
attributes from *synchronous* validator code, where a lazy load has no
greenlet to run in:

```
pydantic_core.ValidationError: 1 validation error for DocumentRead
updated_at
  Error extracting attribute: MissingGreenlet: greenlet_spawn has not been
  called; can't call await_() here.
```

Confirmed on `POST .../documents`, `PATCH .../namespaces/{id}` and
`PATCH .../collections/{id}` — i.e. most of the write surface. Document upload
was the worst case: the row and its job committed, so the document really was
queued and really did get indexed, and the client was told the upload failed.

`expire_on_commit=False` was already set on the session and does not help —
the expiry comes from the flush that issued the `UPDATE`, not from the commit.
Fixed with `__mapper_args__ = {"eager_defaults": True}` on `Base`, which
fetches server-computed columns through `RETURNING` in the same statement.
PostgreSQL supports `RETURNING` on `UPDATE`, so this costs no extra round trip
and keeps the database clock authoritative — the alternative, moving the
timestamp into Python, would have traded a correct clock for a convenient one.

### 10. The whole conversation surface failed without an LLM key — **high**

FastAPI resolves the entire dependency tree before an endpoint body runs.
`ChatService` is a dependency of *every* conversation endpoint — list, create,
rename, delete — and it asked for a `ChatProvider`, which `get_llm` built
eagerly. A deployment with no LLM key therefore failed on all of them, not
only on answering, contradicting the README's promise that "uploads and search
work without one".

Fixed with `LazyChatProvider`, which defers construction to first real use.
`stream` is deliberately left as a plain method rather than `async def`: the
caller owns the generator and closes it on client disconnect, which is what
cancels the upstream request.

### 11. Creating a conversation required a working model — **medium**

Narrower than the above and surviving the same fix: `ChatService.create`
stamped the row with `self._llm.model`, and merely *asking* the provider for
its name was enough to build it. Starting a conversation makes no model call
and should not need one. Now read from `settings.llm_model`, which is the same
value — the provider is constructed from that setting.

### 12. A misconfigured provider was reported as an internal error — **medium**

`LLMConfigurationError` is mapped to 503 by the answer service, but only for
errors raised *inside* a generation call. Raised anywhere else it reached the
catch-all handler and was returned as `500 internal_error`, "An unexpected
error occurred" — sending an operator hunting for a bug instead of an unset
variable. Now handled explicitly as `503 upstream_misconfigured`, with the
provider's own wording logged and never returned.

### 13. Two of the four container roles were permanently `unhealthy` — **low**

Found by reading `docker ps` during cleanup rather than by any test. The
image carries one `HEALTHCHECK`, written for the `api` role: it curls
`/health` on the API port. The `worker` and `frontend` roles do not serve
that endpoint, so both reported `unhealthy` for as long as they ran, while
working perfectly.

Harmless in this compose file — nothing waits on `service_healthy` for
either — but not harmless in general: `docker ps` misreports the fleet,
monitoring alerts on healthy processes, and autoheal-style tooling would
restart-loop the worker indefinitely.

Fixed per role rather than by weakening the API's check. `frontend` gets
Streamlit's own endpoint (`/_stcore/health`, port 8501); `worker` has its
check disabled, because a worker's liveness genuinely is not an HTTP
question — it holds job leases, and an expired lease is the signal that one
has died. Verified: `frontend` now reports `healthy`, `worker` reports plain
`Up`.

### 14. A failed document could never be deleted — **high**

Reported by the author from the UI on 2026-10-23: clicking **Delete** on a
scanned PDF that had failed extraction returned "An unexpected error
occurred." Reproduced against a real PostgreSQL in minutes; the suite passes
it, because the fakes do not enforce CHECK constraints.

`ck_documents_error_message_matches_status` was a biconditional:

```sql
(status = 'failed') = (error_message IS NOT NULL)
```

It asserts two things and only the first is right. *A failed document must
explain itself* — yes. *Nothing else may carry an error* — too strong, because
deletion moves a document to `deleting` and then `archived` while leaving
`error_message` in place. For a document that had failed, that `UPDATE`
violated the CHECK:

```
psycopg.errors.CheckViolation: new row for relation "documents" violates
check constraint "ck_documents_error_message_matches_status"
```

So **no failed document could ever be deleted** — precisely the ones a user
most wants gone. A scan that could not be OCR'd was unremovable for the life
of the deployment, and the only reported symptom was a generic 500.

Clearing the message on delete would also satisfy the old rule and needs no
migration. It is the wrong trade: it destroys the answer to "why is this
gone?" at the moment that question is being asked. Migration `0004` narrows
the constraint instead, so it governs the live states and stops governing a
tombstone — and keeps the `ELSE` branch, because a `ready` document carrying
a stale error is a real defect that nothing else would catch.

Verified end to end on a real server: `DELETE` now returns 204, the document
leaves the listing and 404s directly, the tombstone retains its explanation,
and the four-way truth table still behaves — `ready`+error rejected,
`failed`+NULL rejected, `ready`+NULL accepted, `failed`+message accepted.

### 15. A transient rate limit killed the conversation turn — **high**

Reported from the UI on 2026-10-23:

```
openrouter failed mid-stream: Error code: 429 - {'error': {'message':
'OpenRouter could not verify available credits for this request in time.
Retry shortly.', 'code': 429, 'metadata': {'limit_source':
'openrouter_admission_control', 'headers': {'Retry-After': '10'},
'provider_name': None}}}
```

`provider_name: None` places this at OpenRouter's admission control, before
the request reached a model — so it is an account-level check, not a property
of the model, and switching models does not help.

The defect is that the service did not retry it. `complete()` retries
`RateLimitError`; `stream()` retried nothing, by an explicit decision whose
reasoning was only half right:

> No retry wraps the stream. Once the first token has been handed to the
> client, a retry would restart the answer mid-sentence.

True after the first token. Before it, nothing has been shown to the user and
a retry is invisible — which is exactly what the non-streaming path already
does. The provider was *asking* to be retried, with a `Retry-After: 10`
header, and a single transient 429 became a dead turn.

`stream()` now retries while `emitted` is False and stops the moment a delta
has gone out. `Retry-After` is honoured when the provider sends a usable one,
capped at `MAX_RETRY_AFTER_SECONDS` so a remote system cannot pin a request
open; otherwise the same exponential-with-jitter curve as `retry_async`.
Configuration errors are still never retried.

**Not verified against a live 429.** Six tests cover the boundary, including
that a failure *after* the first token is not retried, and they fail when the
retry is removed. But 38 concurrent live streams through OpenRouter all
succeeded, so the real rate limiter could not be provoked on demand; the
live path remains tested only in simulation.

### 16. The raw provider error was stored and rendered to the user — **medium**

Found while reading the screenshot for defect 15: the UI displayed
OpenRouter's entire error payload. The service's stated contract is the
opposite —

> The provider's own wording is logged but never returned: upstream errors
> routinely quote the request back, which here would mean echoing the prompt
> - and with it the retrieved passages - to the client.

— and the live SSE `error` event does honour it, carrying the translated
`AppError.message`. The leak was on a second path: `stream_turn` stored
`outcome.error = str(exc)`, that string was persisted on the message row, and
`frontend/views/chat.py` renders it when replaying history. So the sanitised
message was shown once and the raw one every time afterwards.

Harmless in this instance — a rate-limit payload — but the class is not: the
same line would persist and re-display a 400 that quoted the prompt back,
including the retrieved passages. Now stores the translated message; the raw
text stays in the logs, joined to the turn by the request id.

### Also: migration 0003 failed the first time it was executed

Not a defect in the application, but a direct refutation of this document's
earlier claim that rendering a migration offline verifies it. `0003` was
checked with `alembic upgrade 0002:head --sql`, which renders SQL without
running it. Executed against a real database it failed:

```
psycopg.errors.DependentObjectsStillExist: cannot drop index
uq_namespaces_slug because constraint uq_namespaces_slug on table
namespaces requires it
```

Revision `0001` created those as `UniqueConstraint`s, and PostgreSQL backs
every unique constraint with an index of the same name, which `DROP INDEX`
refuses to remove. Fixed by dropping the constraint first; now verified by a
full `upgrade → downgrade → upgrade` round trip against a real server. **The
`--sql` row has been removed from the verification table above**, because it
certified nothing.

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

### 2. No test runs against live PostgreSQL — the most expensive gap here

This is no longer a theoretical risk. On 2026-10-23 it cost four defects,
[all of them 500s](#defects-found-by-running-it-2026-10-23), one critical and
affecting most of the write surface. Every one passed the full suite; every
one appeared within minutes of pointing the image at a real database. The
suite's own green result was, for this class of bug, actively misleading.

The fakes can drift from the real SQLAlchemy repositories, and what they
cannot model is precisely what is most worth testing: constraint violations,
cascade behaviour, server-side defaults and `onupdate` (defect 9 lived exactly
there), the `FOR UPDATE SKIP LOCKED` claim under contention, and the partial
indexes added in `0003`.

`tests/test_schema.py` pins the table set, and
`tests/test_live_sql_regressions.py` asserts the properties the four fixes
established — but a property guard only catches a *revert*, never a new
instance of the same class. The real answer is testcontainers in CI, running
the existing suite a second time against a real server. Until that exists,
**a green suite is not evidence that a write path works**, and any change to
a model's columns, mapper arguments or response serialization should be
exercised against a real database by hand before it is trusted.

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

## Verifying the live schema

`alembic current` reports the revision Alembic *recorded*, not the objects it
produced — and `0003` is a revision whose whole purpose is to change the shape
of two indexes. Reading them back is the check that actually closes the loop.
Run this against the migrated database; it is read-only:

```sql
-- Both rows must end in: WHERE (deleted_at IS NULL)
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE indexname IN ('uq_namespaces_slug', 'uq_collections_namespace_id_slug');

-- Must return no rows: the old global constraints should be gone, not
-- merely shadowed. A constraint here means 0003's drop silently did nothing
-- and slug reuse after a soft delete is still impossible.
SELECT conname
  FROM pg_constraint
 WHERE conname IN ('uq_namespaces_slug', 'uq_collections_namespace_id_slug');
```

Expected: two `CREATE UNIQUE INDEX ... WHERE (deleted_at IS NULL)` rows from
the first query, zero rows from the second. That is the state verified on a
throwaway PostgreSQL 17, including across a full
`upgrade → downgrade → upgrade` round trip.

---

## Things the author should act on

1. **Rotate both API keys in `.env` — the OpenAI one and the OpenRouter one.**
   Both were exposed in working-session output. Rotating them is the only
   remedy; nothing else in this report substitutes for it. The OpenRouter key
   is still live: it was used on 2026-10-23 to verify model behaviour, and
   answered.
2. **Apply migration `0004` to the Supabase instance.** `0002` and `0003`
   were applied there on 2026-10-23, but `0004_deletable_failed_documents`
   postdates that run and is what makes a failed document deletable at all —
   until it is applied, every `DELETE` of a failed document returns 500 on
   that database. `alembic upgrade head`. Also worth the one query that reads
   back `0003`'s objects; see
   [verifying it yourself](#verifying-the-live-schema).
3. **Add testcontainers to CI.** With item 2 done, this is the most valuable
   thing left on the list by a wide margin — it is placed below item 1 only
   because a leaked key is urgent in a way that tooling is not. See
   [gap 2](#2-no-test-runs-against-live-postgresql--the-most-expensive-gap-here)
   for what its absence cost in a single afternoon.
4. **Run `docker compose up` once, with a `.env` that does not point at
   production.** The four roles are each verified; their composition — the
   `depends_on` ordering, the shared `rag-data` volume, the one-shot
   `migrate` service — is not. Still worth a throwaway `DATABASE_URL`: the
   migration chain is at head now, so `migrate` would be a no-op, but
   `compose up` also starts an API and a worker that would read and write
   that database, and the compose file's Postgres service is not where your
   data lives.
5. **Decide the embedding model deliberately, and then leave it alone.** A
   collection's vector width is fixed when it is created, so changing
   `EMBEDDING_MODEL` to a model of a different width strands every existing
   collection. `nvidia/nemotron-3-embed-1b` and its `:free` variant are now
   in the registry at their verified 2048 dimensions.
6. **Avoid reasoning models for `LLM_MODEL` unless `LLM_MAX_TOKENS` is raised
   to match.** OpenRouter counts reasoning tokens against `max_tokens`.
   Measured on 2026-10-23: `stepfun/step-3.5-flash` spent 15 of a 16-token
   budget on hidden reasoning and returned `content: null`, which
   `openai_compatible.py` coerces to `""` — a silently empty answer that
   looks like a broken application rather than an exhausted budget.
   `stepfun/step-3.7-flash` and `anthropic/claude-sonnet-5.5` use no
   reasoning tokens and answer correctly, with citation markers intact, at
   the default 1024.
