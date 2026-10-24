# Orbital Customer FAQ

Owner: Customer Support, Helix Labs
Last updated: 2 October 2026

## Accounts and access

### How do I reset my password?

Choose "Forgot password" on the sign-in page. The reset link is valid for
one hour and can be used once. If your workspace uses single sign-on,
password reset does not apply; your identity provider owns your password.

### I've been locked out of my workspace. What now?

Ask another Owner of the workspace to restore your access. If there is no
other Owner reachable, contact support from the email address on the
account; we will verify ownership with the billing contact before making any
change, which usually takes one working day.

### Can I move a pipeline from one workspace to another?

Not directly. Export the pipeline definition from the source workspace and
import it into the target. Connections do not transfer, because credentials
are encrypted with a per-workspace key; you will recreate them in the
target workspace.

### How many workspaces can I have?

There is no limit on the Standard and Enterprise plans. The Starter plan is
limited to one workspace.

## Pipelines and runs

### Why was my scheduled run skipped?

Because the previous run of the same pipeline was still going when this one
was due, and the pipeline's overlap behaviour is set to "Skip". Skipped runs
appear in the run history with the reason, as of release 2026.3.4.

If every run is being skipped, the pipeline is taking longer than its
interval. Either lengthen the interval or find out why the runs are slow.

### Can I re-run just one failing step?

Yes. Open the run, select the failing step, and choose "Retry from here".
Steps before it are not re-run; their outputs are reused. This works for any
run, not only the most recent.

### What's the shortest schedule interval?

Five minutes, as of release 2026.3.0. Pipelines that were on a one-minute
interval before that release were moved to five minutes automatically.

### Why does my schedule run at a different time than I expected?

Schedules always run in UTC, regardless of the time zone shown in your
display settings. This is deliberate, so a schedule does not shift by an
hour when daylight saving changes.

### How long is run history kept?

90 days on the Standard plan and up to 365 days on Enterprise, configurable
by a workspace Owner. The audit log is kept for 400 days on every plan and
is separate from run history.

## Data and documents

### What file types can I upload?

PDF, DOCX, plain text and Markdown. The type is detected from the file's
contents, not its name, so renaming a file to change its extension does not
work.

### My PDF uploaded but no text was found.

The PDF is almost certainly a scan: page images with no text layer. Orbital
does not perform OCR. Run OCR on the file yourself and upload the result.

### Why was my upload rejected as a duplicate?

A file with identical contents is already in that collection.
Deduplication is by content, not filename. Upload it to a different
collection, or use the existing document.

### How large can an upload be?

25 MB per file by default. Enterprise workspaces can raise this; ask your
account team.

## Billing

### How is usage measured?

By pipeline run-minutes: the total time your pipelines spend executing. The
number of pipelines, connections and members does not affect the bill. A
pipeline that exists but never runs costs nothing.

### What happens if I exceed my plan's included usage?

Runs continue. The overage appears on the next invoice at the rate in your
plan. Workspace Owners get an alert at 80 percent and at 100 percent of
included usage; nothing is ever cut off without notice.

### Can I get an invoice in a different currency?

Invoices are issued in EUR, USD or GBP. Changing currency takes effect at
the next billing period, not retrospectively.

## Support

### What are your support hours?

Standard plan: Monday to Friday, 09:00 to 18:00 CET, with a first response
within one working day. Enterprise: 24 hours a day for Severity 1, with a
first response within one hour.

### How do I report a problem effectively?

Include the request identifier from the failing response, the workspace
identifier, the time the problem happened with its time zone, and what you
expected to happen. That is usually enough for a first reply that is an
answer rather than a question.
