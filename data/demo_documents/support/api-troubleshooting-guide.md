# API Troubleshooting Guide

Owner: Customer Support, Helix Labs
Audience: support engineers handling Orbital API tickets

Organised by what the customer reports, not by what is wrong, because the
customer tells you the symptom.

## Before anything else: get the request identifier

Every Orbital API response carries an `X-Request-ID` header, and every error
body repeats it. Ask for it in your first reply. With it, a diagnosis takes
minutes; without it, you are searching logs by timestamp and guessing.

If the customer cannot produce one, ask them to make the failing call again
and capture the response headers.

## "I'm getting 401 on every call"

Three causes, in order of how often we see them.

1. **The token has expired.** Access tokens last 30 minutes. A script that
   fetches a token at start-up and then runs for an hour will fail halfway
   through. The fix is to refresh on 401 and retry once.
2. **The header is malformed.** The header must be
   `Authorization: Bearer <token>`. A missing "Bearer ", a newline captured
   from a copy-paste, or the whole JSON response pasted instead of the token
   field all produce 401.
3. **The token is for a different environment.** A token issued by the
   sandbox is not valid against production. The two have different base
   URLs and this is easy to miss when copying a script.

## "I'm getting 404 but the pipeline definitely exists"

In Orbital, a request for a resource in a workspace the caller is not a
member of returns 404, not 403. This is intentional: a 403 would confirm
that the identifier exists, which lets anyone enumerate workspace and
pipeline identifiers.

So check membership first. Ask which workspace the API token belongs to and
which workspace the pipeline is in. In the great majority of these tickets
they are different, usually because the customer has more than one workspace
and generated the token in the wrong one.

If membership is correct, check whether the resource was deleted. Deleted
resources return 404, and the audit log will show who deleted it and when.

## "Uploads are rejected as an unsupported type"

Orbital determines file type by inspecting the first bytes of the file, not
by the extension. A file named `report.pdf` that is actually a renamed
spreadsheet is rejected, which is the intended behaviour.

Ask the customer to confirm the real type. On macOS or Linux, `file
report.pdf` answers it immediately.

Supported types are PDF, DOCX, plain text and Markdown. Older `.doc` files
are not supported; the customer must save as `.docx` first.

## "The upload succeeded but there's no text in it"

The document was accepted and parsed, and the parse produced nothing. Almost
always this is a PDF made of scanned page images rather than text. Orbital
does not perform OCR.

To confirm: ask the customer to open the PDF and try to select a sentence
with the cursor. If nothing selects, there is no text layer.

The workaround is for the customer to run OCR themselves before uploading.

## "I get a 409 conflict when uploading"

Deduplication is by content hash within a collection. The customer has
already uploaded a byte-for-byte identical file. Renaming the file does not
change the hash and will not avoid the conflict.

Point them at the existing document; the conflict response includes its
identifier. If they genuinely need a second copy, it has to go in a
different collection.

## "Everything is slow and I'm seeing 429s"

Check the `Retry-After` header on the 429 response and ask what the client
does with it. Clients that ignore it and retry immediately make their own
rate limiting worse, and a tight retry loop can keep an account limited
indefinitely.

Correct client behaviour is to wait the number of seconds the header gives,
then retry, with a cap so a very large value does not hang the client.

## "Answers come back empty"

Only applies to self-hosted installations. The usual cause is a model that
spends its entire token budget on internal reasoning before producing any
answer text, which arrives as an empty answer rather than an error.

Ask for the configured model and the configured maximum token setting. Raise
the maximum substantially, or change to a model that does not use reasoning
tokens, and the answer appears.

## When to escalate

Escalate to Platform Engineering when: the error is a 500 and you have a
request identifier; the same symptom appears for more than one customer
within an hour; or any answer in this guide does not match what you are
seeing. An answer that no longer matches reality is itself worth a ticket.
