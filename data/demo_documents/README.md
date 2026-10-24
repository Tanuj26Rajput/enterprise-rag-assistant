# Demo documents

Seventeen fictional documents, authored in Markdown, grouped by the workspace
they are seeded into.

```
engineering/   5 documents   -> Engineering Documentation (demo-engineering)
hr/            4 documents   -> HR Policies              (demo-hr)
product/       4 documents   -> Product Documentation    (demo-product)
support/       4 documents   -> Customer Support KB      (demo-support)
```

## Everything here is invented

The company (Helix Labs), its product (Orbital), the people named in the
accounts, the policies, the plan prices and the release notes do not exist
and never did. Nothing was copied from a real company's documentation, and
no document contains confidential or copyrighted material.

Content is static. The seeder does not call a model to write any of it.

## Why Markdown rather than committed PDFs

Each document declares a target format — PDF, DOCX or TXT — in
`scripts/demo/catalog.py`, and is rendered into that format at seed time by
`scripts/demo/rendering.py`.

Two reasons. A text-only repository stays reviewable: changing a sentence in
a demo document shows up in a diff as a changed sentence rather than as a
new binary blob. And rendering with `pymupdf` and `python-docx` — the same
libraries `app/ingestion/extraction.py` reads with — guarantees the uploaded
bytes are a genuine PDF or OPC package. Upload validation checks magic bytes
rather than the extension, so a file that merely ends in `.pdf` is rejected.

The mix is deliberate: PDFs exercise the page-number path through chunking
and into citations, which DOCX and plain text have no equivalent of.

| Format | Count | Extracted with |
|---|---|---|
| PDF | 6 | `pymupdf`, with page numbers |
| DOCX | 4 | `python-docx`, single page |
| TXT | 7 | decoded as UTF-8, single page |

## Editing

Edit the Markdown. The next seed run into a *fresh* collection picks up the
change.

Re-running the seeder against a collection that already holds the document
will **not** replace it — the seeder never overwrites anything. To re-ingest
an edited document, delete it through the API first, or seed into a new
collection.

## Adding a document

1. Write the Markdown file under the right folder.
2. Add a `DemoDocument(source, title, fmt)` entry to that namespace's
   collection in `scripts/demo/catalog.py`.
3. Add the questions it answers to `docs/demo-scenarios.md`.

The renderer understands headings, bullets, numbered items, table rows and
paragraphs. Anything beyond that is rendered as a paragraph, which is fine
for a demo corpus and is not worth a full Markdown implementation.

## Related

- [docs/demo-credentials.md](../../docs/demo-credentials.md) — accounts and
  who can see which workspace.
- [docs/demo-scenarios.md](../../docs/demo-scenarios.md) — what to ask.
