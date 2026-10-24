# Orbital Release Notes, Q3 2026

Releases 2026.3.0 through 2026.3.4

## 2026.3.4 (24 September 2026)

### Fixed

- Pipelines with more than 200 steps could time out while rendering the run
  view. The run view now loads step detail lazily and opens in under a
  second regardless of pipeline size.
- The Retry-After header was absent on some rate-limited API responses,
  which made correct client backoff impossible. It is now always present on
  a 429.
- Scheduled runs skipped because of the "Skip" overlap setting were not
  appearing in the run history at all. They now appear with the status
  "skipped" and the reason.

## 2026.3.3 (10 September 2026)

### Added

- **Audit log export.** Workspace Owners can export the audit log for any
  period up to 90 days as newline-delimited JSON. The export includes the
  actor, the action, the affected resource, the source address and the
  request identifier.
- Connector credential rotation without pipeline downtime. A connection can
  now hold two credentials during a rotation window; the new one is used for
  new runs while in-flight runs finish with the old one.

### Changed

- The default pipeline timeout for new pipelines is now three times the
  median of the last ten runs, instead of a flat 60 minutes.

### Fixed

- A connection test against a source returning a redirect reported "unknown
  error" rather than following the redirect.

## 2026.3.2 (27 August 2026)

### Added

- Step-level retry policies. Each step can now set its own retry count and
  backoff, overriding the pipeline default. Previously a single flaky step
  forced the whole pipeline's retry policy to be loosened.

### Changed

- **Breaking for API clients.** The `GET /api/v1/runs` endpoint now returns
  at most 100 items per page, reduced from 1,000, and always returns a
  `next_cursor`. Clients that assumed a single page returned everything must
  follow the cursor. This was announced on 1 July 2026.

### Deprecated

- The `filter` query parameter on `GET /api/v1/pipelines` is deprecated in
  favour of `q`. It continues to work and will be removed no earlier than
  2027.1.

## 2026.3.1 (13 August 2026)

### Fixed

- Workspace Viewers could see the "Run now" button. The button was disabled
  server-side, so no Viewer was ever able to start a run, but the control
  should not have been visible.
- Pipeline names containing emoji broke CSV export of run history.
- A failed run's error message was truncated at 256 characters in the
  notification email while the run view showed the full text.

## 2026.3.0 (30 July 2026)

### Added

- **Branching pipelines.** A step can now have multiple downstream steps,
  which run in parallel, and a join step that waits for all of them. This
  replaces the common workaround of three near-identical pipelines on the
  same schedule.
- **Dry run.** Any pipeline can be executed in dry-run mode, which performs
  every read, resolves every transform, and writes nothing to the
  destination. The run view shows the rows that would have been written.
- Search across pipeline names, step names and connection names from a
  single box.

### Changed

- The minimum supported schedule interval is now 5 minutes, raised from 1
  minute. Pipelines on a shorter interval were migrated to 5 minutes on
  upgrade and their owners were notified.
- Run history retention on the Standard plan is now 90 days, up from 30.

### Removed

- The legacy YAML pipeline importer, deprecated in 2025.4, has been removed.
  Export to the current format before upgrading if you still have YAML
  definitions.

## Upgrade notes

2026.3.0 includes an additive schema migration that takes approximately two
minutes per 10 million run records. The migration runs online; pipelines
continue to execute during it.
