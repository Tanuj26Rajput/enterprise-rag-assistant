# Trade-offs

Short explanations of why key technologies and design decisions were chosen,
in plain language.

**Why FastAPI and PostgreSQL.** FastAPI gives async request handling,
automatic validation (Pydantic) and auto-generated API docs, which matters
for a service with this many endpoints. PostgreSQL is used as the single
source of truth for everything structured — users, namespaces, documents,
jobs and conversations — because it is reliable, supports transactions, and
is what almost every team already knows how to operate.

**Why ChromaDB for vector search.** Chroma is a real vector database that
also runs *embedded* — no second server needed in local development, while
still persisting data to disk and supporting metadata filtering (so a search
can be scoped to one tenant). For a demo-sized corpus (thousands of chunks)
it is a good, low-effort fit. The trade-off: the embedded mode is
single-writer and has no built-in authentication, so at a larger scale the
right next step is running Chroma as its own server (`CHROMA_HOST`) or
moving to a managed vector database.

**Why an external LLM provider (OpenRouter).** Running a capable chat model
yourself is expensive and operationally heavy. OpenRouter gives access to
many models behind one API, so the model used for answering can be changed
with one environment variable (`LLM_MODEL`) instead of a code change.

**Why document chunks and embeddings are needed.** A whole document is too
large to pass to a language model, and a model can't "search" a document on
its own. So every document is split into smaller chunks, each chunk is
turned into a vector embedding, and a question is answered by first finding
the chunks whose embeddings are closest to the question's embedding. This is
the "retrieval" half of retrieval-augmented generation (RAG).

**Why embeddings are configured separately from chat.** OpenRouter mainly
hosts *chat* models, and most of them have no embeddings endpoint at all. So
`EMBEDDING_PROVIDER` and `LLM_PROVIDER` are two independent settings. Mixing
embeddings from two different models in one collection isn't blocked by an
error — it just silently returns bad results, because the two sets of
vectors aren't comparable. That's why a collection's embedding model and
dimension are fixed once the collection is created.

**How citations improve answer traceability.** The model is only allowed to
write citation *markers* like `[1]`, never a document name or page number
itself. The backend then resolves each marker against the exact passages it
actually sent to the model. A marker that doesn't match a real, retrieved
passage is removed from the answer. This means a citation the user sees is
always real — the model cannot invent a source. If no retrieved passage
actually supports an answer, the system says "I don't know" instead of
guessing.

**Why namespace-level authorization matters.** Every team's documents live
in their own namespace, and every request that touches a namespace's data
checks the caller's membership and role first. A user who isn't a member of
a namespace gets a 404 (not a 403), so they can't even confirm the
namespace exists. This is what keeps one customer's documents from leaking
into another's search results.

**Why PostgreSQL is also the job queue.** Document processing (extract text,
chunk, embed, index) happens as a background job stored as a row in
PostgreSQL, rather than in a separate queue system like Redis or SQS. The
job and the document it affects are updated in the same database
transaction, so there is no scenario where "the job finished" and "the
document is marked ready" disagree with each other. The cost is that this
approach doesn't scale as well as a dedicated queue at very high job
volumes — acceptable for the size of system this project targets.

## Main limitations

- **Tenant isolation is enforced in application code**, not by database-level
  row security. A bug in a repository method could in principle leak data
  across tenants; there is no second line of defense at the database layer
  yet.
- **ChromaDB (embedded mode) supports only one writer process** and has no
  authentication of its own — acceptable for this project's scale, not for
  a large multi-server deployment.
- **No automated tests run against a real PostgreSQL instance** — the test
  suite uses an in-memory fake for the database layer, which can miss bugs
  that only appear against real SQL behavior.
- **No rate limiting** is implemented in the application itself; it is
  expected to be handled by whatever sits in front of it in production.
- **No retrieval reranking, response caching, or production monitoring
  dashboard** are implemented — only what's described above.

## Possible future improvements

- Move tenant isolation into PostgreSQL row-level security as a second line
  of defense.
- Run ChromaDB as its own server instead of embedded, to remove the
  single-writer limit.
- Add integration tests against a real PostgreSQL database.
- Add basic rate limiting and a reranking step after retrieval.
