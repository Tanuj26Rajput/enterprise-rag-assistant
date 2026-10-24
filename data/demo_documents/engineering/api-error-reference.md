# Orbital API Error Reference

Owner: Platform Engineering, Helix Labs
Applies to: Orbital API v1

## The error envelope

Every error response from the Orbital API has the same shape. There is no
endpoint that reports a failure in a different format, which means a client
needs exactly one error-handling path.

    {
      "error": {
        "code": "pipeline_not_found",
        "message": "Pipeline not found.",
        "request_id": "01JB5T4K9W2QX7N3F8ZCVH6MRD"
      }
    }

The code is stable and safe to branch on. The message is written for a human
and may change between releases, so clients must not match on it. The
request identifier is the same one that appears in our logs and in the
X-Request-ID response header; quoting it in a support ticket reduces a
diagnosis from hours to minutes.

## 400 validation_failed

The request body did not pass validation. The message names the first field
that failed. Retrying the identical request will fail identically, so a
client must not retry a 400.

## 401 authentication_required

No credentials were supplied, or the access token has expired. Access tokens
are valid for 30 minutes. The correct response is to refresh the token once
and retry; if the refresh also returns 401, the session is over and the user
must sign in again.

## 403 permission_denied

The caller is authenticated but their role in this workspace does not permit
the action. Note the deliberate asymmetry with 404 below: 403 means "you may
read this workspace but not do this", while an attempt to touch a workspace
you are not a member of returns 404, not 403. Returning 403 there would
confirm that the workspace exists, which lets an attacker enumerate
workspace identifiers.

## 404 not_found

The resource does not exist, or it exists and the caller has no access to
the workspace that contains it. These two cases are deliberately
indistinguishable.

## 409 conflict

The request collided with existing state. The two common causes are a slug
that is already taken within the workspace, and uploading a file whose
contents are byte-for-byte identical to a file already in the collection.
Deduplication is by content hash, not by filename, so renaming a file before
re-uploading it does not avoid the conflict.

## 413 payload_too_large

The upload exceeded the configured maximum, which is 25 MB by default. The
limit is checked before the body is read into memory, so a client gets this
response quickly rather than after a long upload.

## 415 unsupported_media_type

The uploaded file is not a type Orbital can parse. Supported types are PDF,
DOCX, plain text and Markdown. The type is determined by inspecting the
first bytes of the file, not by the filename extension; renaming a
spreadsheet to .pdf produces this error, which is the intended behaviour.

## 422 unprocessable_entity

The request was well formed and the file type was supported, but the content
could not be used. The usual cause is a PDF consisting only of scanned
images, which contains no extractable text. Orbital does not perform OCR.

## 429 rate_limited

Too many requests. The Retry-After response header gives the number of
seconds to wait and should be honoured rather than guessed at; blind
exponential backoff either retries too early and wastes the attempt, or far
too late.

## 500 internal_error

Something failed that we did not anticipate. The response never contains a
stack trace or any internal detail. The request identifier is the only thing
worth recording, and it is enough for us to find the failure.

## 503 service_unavailable

A dependency Orbital needs is unavailable or misconfigured. The most common
cause in a self-hosted installation is a missing or rejected model provider
credential: uploads and search continue to work, and only answering fails.
This is a safe status to retry with backoff.
