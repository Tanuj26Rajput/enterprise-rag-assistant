# Enterprise Multi-Document RAG Assistant

## 1. Project overview

A multi-tenant Retrieval-Augmented Generation (RAG) assistant. Teams create
isolated workspaces, upload documents, and ask questions in plain English.
The system retrieves the relevant passages and answers with real citations
— or says it doesn't know, instead of guessing.

Built with FastAPI, PostgreSQL, ChromaDB, OpenRouter and Streamlit.

---

## 2. Main features

- **Multi-tenant workspaces** ("namespaces") with four roles: owner, admin,
  member, viewer. One team's documents are never visible to another's.
- **Document ingestion** for PDF, DOCX, TXT and Markdown, with real file-type
  validation (checked by file content, not just the extension).
- **Retrieval-augmented answers with real citations.** The model can only
  reference passages it was actually given; the backend resolves and
  verifies every citation before it reaches the user.
- **"I don't know" when appropriate.** If the retrieved documents don't
  support an answer, the system says so instead of making one up.
- **Streaming responses** over Server-Sent Events (SSE), so answers appear
  token by token like a chat interface.
- **Resilient ingestion.** Uploads are processed as background jobs with
  retries and failure tracking, so a bad file doesn't crash anything.
- **Basic observability.** Structured logs and Prometheus-style metrics.

---

## 3. Technology stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI (Python, async) |
| Database | PostgreSQL (via SQLAlchemy + Alembic migrations) |
| Vector search | ChromaDB |
| Embeddings | OpenAI, OpenRouter, or a local deterministic "hash" provider for testing |
| Chat / answers | OpenRouter (any compatible chat model) |
| Frontend | Streamlit |
| Auth | JWT access + refresh tokens, Argon2 password hashing |

---

## 4. Simple architecture overview

```
User → Streamlit UI → FastAPI backend → PostgreSQL (users, documents, jobs)
                                        → ChromaDB (vector search)
                                        → Embedding provider (for embeddings)
                                        → OpenRouter (for chat answers)
```

The Streamlit UI never talks to the database or any AI provider directly —
it only calls the FastAPI backend, and holds no secrets. Every decision
about who can see what is made in the backend.

A simple diagram of this flow is in
[docs/diagrams/architecture.md](docs/diagrams/architecture.md). More
diagrams (deployment, components, database schema, and sequence diagrams)
are in [docs/diagrams/](docs/diagrams/).

---

## 5. Local setup and environment variables

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env             # then edit it — see below
```

Three variables have no default and must be set:

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Your PostgreSQL connection string |
| `SECRET_KEY` | Signs login tokens — generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `OPENROUTER_API_KEY` or `OPENAI_API_KEY` | Needed to generate answers (uploads and search still work without one) |

A couple of others worth knowing:

| Variable | Default | What it controls |
|---|---|---|
| `INGESTION_MODE` | `inline` | `inline` processes uploads immediately; `background` uses a separate worker process |
| `EMBEDDING_PROVIDER` | `openai` | `openai`, `openrouter`, or `hash` (free, offline, used for tests) |

Every other variable, with its default and what happens if it's wrong, is
documented in [docs/environment.md](docs/environment.md).

---

## 6. Database migrations

The schema is managed entirely by Alembic — the application never creates or
changes tables on its own.

```bash
alembic upgrade head        # apply all pending migrations
alembic current             # check which migration is currently applied
```

Run this once after setting `DATABASE_URL`, and again any time you pull
changes that include a new migration file under `migrations/versions/`.

---

## 7. Running the backend and the Streamlit UI

```bash
python run.py                    # backend API → http://127.0.0.1:8000/docs
streamlit run frontend/app.py    # UI          → http://localhost:8501
python -m app.worker             # only needed if INGESTION_MODE=background
```

`run.py` is used instead of `uvicorn app.main:app` directly because of a
Windows-specific event loop issue; on Linux/macOS, `uvicorn app.main:app`
works the same way.

### Try it in two minutes

```bash
API=http://127.0.0.1:8000/api/v1

TOKEN=$(curl -s -X POST $API/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-battery-staple"}' >/dev/null &&
  curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct-horse-battery-staple"}' | jq -r .access_token)

NS=$(curl -s -X POST $API/namespaces -H "Authorization: Bearer $TOKEN" \
  -d '{"slug":"acme","name":"Acme"}' | jq -r .id)

COL=$(curl -s -X POST $API/namespaces/$NS/collections -H "Authorization: Bearer $TOKEN" \
  -d '{"slug":"handbook","name":"Handbook"}' | jq -r .id)

curl -s -X POST $API/namespaces/$NS/collections/$COL/documents \
  -H "Authorization: Bearer $TOKEN" -F "file=@handbook.pdf"

curl -s -X POST $API/namespaces/$NS/collections/$COL/answer \
  -H "Authorization: Bearer $TOKEN" -d '{"question":"How much annual leave carries over?"}' \
  | jq '{answer, citations}'
```

### Or load ready-made demo data

To explore the app without preparing your own files, seed a fictional demo
dataset (3 accounts, 4 workspaces, 17 documents):

```bash
alembic upgrade head
python -m scripts.seed_demo_data --yes
```

Sign in at <http://localhost:8501> with one of the accounts listed in
[docs/demo-credentials.md](docs/demo-credentials.md), and see
[docs/demo-scenarios.md](docs/demo-scenarios.md) for example questions to
ask.

---

## 8. How document ingestion and RAG work

**Ingestion (when you upload a document):**

1. The file's type is validated by its actual content (not just its
   extension), and it's stored.
2. The text is extracted (with page numbers, where the format has them).
3. The text is split into overlapping chunks small enough for the model to
   use.
4. Each chunk is turned into a vector embedding and stored in ChromaDB,
   alongside the original text in PostgreSQL.

**Answering a question (RAG):**

1. The question is embedded the same way as the document chunks.
2. ChromaDB returns the most similar chunks, scoped to the user's workspace.
3. Those chunks and the question are sent to the chat model, which is
   instructed to answer **using only those passages** and to mark its
   sources as `[1]`, `[2]`, etc.
4. The backend matches each marker back to the real chunk it refers to and
   attaches the document name, page number and excerpt. A marker that
   doesn't match a real, retrieved passage is removed from the answer.
5. If nothing retrieved actually supports an answer, the response says the
   documents don't contain an answer, instead of guessing.

See [docs/diagrams/sequence-diagrams.md](docs/diagrams/sequence-diagrams.md)
for a step-by-step diagram of both flows.

---

## 9. Docker instructions

```bash
cp .env.example .env        # set SECRET_KEY and one provider API key
docker compose up --build
```

This starts PostgreSQL, applies migrations, then starts the API, a
background worker, and the Streamlit frontend.

→ UI at <http://localhost:8501>, API docs at <http://localhost:8000/docs>.

Use a `.env` pointing at a database you don't mind modifying — migrations
run automatically when the containers start. See
[docs/diagrams/deployment.md](docs/diagrams/deployment.md) for what each
container does and what is *not* part of this deployment (e.g. a production
database or a load balancer).

---

## 10. Deploying to Streamlit Community Cloud (single process)

Streamlit Cloud only runs one process on one exposed port, so the backend
can't be deployed separately as it is with Docker above. `streamlit_app.py`
at the repo root works around that: it starts the FastAPI backend as a
background subprocess on loopback, waits for it to become healthy, then
renders the same Streamlit UI from `frontend/app.py` in the foreground.

1. Push this repo to GitHub, then create an app on
   [share.streamlit.io](https://share.streamlit.io) pointing at it, with
   **Main file path** set to `streamlit_app.py` (not `frontend/app.py`).
2. `requirements.txt` already covers both the backend and the frontend, so
   no changes are needed there.
3. In the app's **Settings → Secrets**, paste values based on
   [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example) — at
   minimum `SECRET_KEY`, `DATABASE_URL`, and one LLM + one embedding provider
   key. `streamlit_app.py` copies every secret into the process environment,
   where `app/core/config.py` reads it exactly as it would a `.env` file.
4. `DATABASE_URL` must point at a database reachable from the internet —
   Streamlit Cloud has no database of its own. A free
   [Supabase](https://supabase.com) or [Neon](https://neon.tech) Postgres
   project both work; migrations run automatically on startup.

Two limitations are inherent to a single free-tier container, not to this
entrypoint: the local Chroma store (`./var/chroma`) lives on ephemeral disk
and is wiped on restart (point `CHROMA_HOST`/`CHROMA_PORT` at a hosted Chroma
instance to avoid that), and there is no separate background worker, so
`INGESTION_MODE` should stay `inline` (the default).

---

## 11. API documentation

- Interactive docs (Swagger UI): `http://localhost:8000/docs`, while the
  backend is running (disabled when `ENVIRONMENT=production`).
- A grouped, human-readable guide: [docs/api.md](docs/api.md) — covers
  authentication, namespaces/collections, document upload, chat/streaming,
  and health checks.

---

## 12. Known limitations

- **Tenant isolation is enforced in application code**, not by
  database-level row security.
- **ChromaDB runs embedded** (not as a separate server) and supports only
  one writer process at a time — fine for this project's scale, not for a
  large production deployment.
- **No automated tests run against a real PostgreSQL database** — the test
  suite uses an in-memory fake for the database layer.
- **No rate limiting** is built into the application itself.
- **No reranking, response caching, or production monitoring dashboard.**

More detail, and the reasoning behind each trade-off, is in
[docs/trade-offs.md](docs/trade-offs.md).

---

## Project structure

```
app/            FastAPI backend (routers, services, database models, etc.)
frontend/       Streamlit UI — talks only to the backend API
migrations/     Alembic database migrations
scripts/        Operator scripts (e.g. demo data seeding)
data/           Demo document corpus
tests/          Automated tests
docs/           Documentation (see below)
```

## Documentation index

| Document | What it covers |
|---|---|
| [docs/diagrams/](docs/diagrams/) | Simple architecture, deployment, components, ER and sequence diagrams |
| [docs/api.md](docs/api.md) | Grouped API reference with examples |
| [docs/environment.md](docs/environment.md) | Every environment variable |
| [docs/trade-offs.md](docs/trade-offs.md) | Why key decisions were made, and their limitations |
| [docs/demo-credentials.md](docs/demo-credentials.md) | Demo accounts and permissions |
| [docs/demo-scenarios.md](docs/demo-scenarios.md) | Example questions for the demo data |
| [docs/architecture.md](docs/architecture.md) | Full detailed architecture (for deeper reading) |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues, by symptom |
| [docs/audit.md](docs/audit.md) | Internal notes on what was tested and what wasn't |
