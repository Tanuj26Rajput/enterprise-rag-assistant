# Entity-relationship diagram (simple view)

The real tables from `app/db/models/`, with only the fields that matter for
understanding the relationships.

```mermaid
erDiagram
    users ||--o{ namespace_members : "is a member via"
    users ||--o{ namespaces : owns
    users ||--o{ conversations : creates
    namespaces ||--o{ namespace_members : has
    namespaces ||--o{ collections : contains
    namespaces ||--o{ documents : scopes
    namespaces ||--o{ conversations : scopes
    collections ||--o{ documents : groups
    documents ||--o{ document_chunks : "split into"
    documents ||--o{ ingestion_jobs : "processed by"
    conversations ||--o{ messages : contains

    users {
        uuid id PK
        string email UK
        string hashed_password
        bool is_superuser
    }

    namespaces {
        uuid id PK
        string slug UK
        uuid owner_id FK
    }

    namespace_members {
        uuid id PK
        uuid namespace_id FK
        uuid user_id FK
        enum role "owner, admin, member, viewer"
    }

    collections {
        uuid id PK
        uuid namespace_id FK
        string slug
        string embedding_model
    }

    documents {
        uuid id PK
        uuid namespace_id FK
        uuid collection_id FK
        string filename
        enum status "pending, queued, processing, ready, failed, deleting, archived"
    }

    document_chunks {
        uuid id PK
        uuid document_id FK
        int chunk_index
        text content
        int page_number
    }

    ingestion_jobs {
        uuid id PK
        uuid document_id
        enum kind "ingest, delete"
        enum status "queued, running, succeeded, failed, cancelled"
    }

    conversations {
        uuid id PK
        uuid namespace_id FK
        uuid user_id FK
        string model
    }

    messages {
        uuid id PK
        uuid conversation_id FK
        int sequence
        enum role "user, assistant, system"
        jsonb citations
    }
```

**How to explain it in one minute**

- A **user** owns and is a member of one or more **namespaces**
  (workspaces), with a role per namespace.
- A namespace contains **collections**, and each collection contains
  **documents**.
- Each document is split into **document_chunks** — the actual text passages
  that get embedded and searched.
- An **ingestion_job** tracks the background work of processing a document
  (so uploads can be retried if they fail, without blocking the request).
- A **conversation** belongs to one user and one namespace, and holds an
  ordered list of **messages**. Each assistant message stores its citations
  — the exact chunks the answer was based on.

This matches the real SQLAlchemy models in `app/db/models/`; no table or
relationship here is invented. For every column, constraint and index, see
the full version in [architecture.md](../architecture.md#3-entity-relationship-model).
