# Sequence diagrams (simple view)

Three flows, kept short enough to narrate out loud.

## 1. Uploading and ingesting a document

```mermaid
sequenceDiagram
    actor U as User
    participant API as FastAPI
    participant DB as PostgreSQL
    participant E as Embedding provider
    participant CH as ChromaDB

    U->>API: Upload file
    API->>API: Check role, validate file type
    API->>DB: Save document (status: queued)
    API-->>U: 201 Created

    API->>API: Extract text, split into chunks
    API->>E: Get embeddings for chunks
    E-->>API: Vectors
    API->>CH: Store vectors
    API->>DB: Save chunks, mark document "ready"
```

## 2. Asking a question (retrieval + citations)

```mermaid
sequenceDiagram
    actor U as User
    participant API as FastAPI
    participant CH as ChromaDB
    participant DB as PostgreSQL
    participant LLM as OpenRouter

    U->>API: Ask a question
    API->>CH: Search for similar chunks
    CH-->>API: Matching chunk IDs
    API->>DB: Load the real chunk text for those IDs
    API->>LLM: Send question + retrieved passages
    LLM-->>API: Answer with [1], [2] markers
    API->>API: Match markers back to the real chunks
    API-->>U: Answer + citations (document, page, excerpt)
```

The citation markers are resolved by the backend against the chunks it
actually sent — the model cannot invent a source that wasn't retrieved.

## 3. Logging in and accessing a namespace

```mermaid
sequenceDiagram
    actor U as User
    participant API as FastAPI
    participant DB as PostgreSQL

    U->>API: Login (email + password)
    API->>DB: Verify password hash
    API-->>U: JWT access token

    U->>API: Request a namespace's data (with token)
    API->>API: Decode token → user
    API->>DB: Is this user a member of this namespace?
    alt member
        DB-->>API: Role (owner / admin / member / viewer)
        API-->>U: Data, filtered to this role's permissions
    else not a member
        API-->>U: 404 Not Found
    end
```

Note that a namespace the user is not a member of returns **404**, not
**403** — this prevents someone from even confirming that a workspace exists
by probing its ID.
