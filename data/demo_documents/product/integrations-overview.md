# Orbital Integrations Overview

Product: Orbital 2026.3

An integration, called a connector in the product, lets a pipeline read from
or write to an external system. This document lists what is available, what
each connector supports, and the limits that most often surprise people.

## Connector capabilities

Every connector declares three capabilities:

- **Read**: can be used as a source step.
- **Write**: can be used as a destination step.
- **Incremental**: can read only records changed since the last successful
  run, rather than the whole dataset.

A connector without Incremental re-reads everything on every run. For a
large dataset on a frequent schedule, that is usually the difference between
a pipeline that is cheap and one that is not.

## Databases

| Connector | Read | Write | Incremental |
|---|---|---|---|
| PostgreSQL | yes | yes | yes, by column watermark |
| MySQL | yes | yes | yes, by column watermark |
| SQL Server | yes | yes | yes, by column watermark |
| Snowflake | yes | yes | yes, by stream |
| BigQuery | yes | yes | yes, by partition |
| MongoDB | yes | yes | yes, by oplog |

Watermark-based incremental reads need a monotonically increasing column,
usually an updated-at timestamp or a sequence. Orbital stores the last
watermark per pipeline, not per connection, so two pipelines reading the
same table keep independent positions.

A watermark cannot detect deletions. If deletions matter, pair the
incremental read with a periodic full reconciliation run on a slower
schedule.

## Object storage and files

| Connector | Read | Write | Incremental |
|---|---|---|---|
| Amazon S3 | yes | yes | yes, by key prefix and modified time |
| Azure Blob Storage | yes | yes | yes |
| Google Cloud Storage | yes | yes | yes |
| SFTP | yes | yes | no |
| HTTP file fetch | yes | no | no |

Supported file formats are CSV, TSV, JSON, newline-delimited JSON, Parquet
and Avro. Compressed variants with gzip, zstd and snappy are detected
automatically from the file contents rather than the extension.

Excel files are not supported as a source. Export to CSV first; a
spreadsheet is a document, and treating one as a dataset reliably produces
surprises about types.

## SaaS applications

| Connector | Read | Write | Incremental |
|---|---|---|---|
| Salesforce | yes | yes | yes |
| HubSpot | yes | yes | yes |
| Zendesk | yes | no | yes |
| Jira | yes | yes | yes |
| Stripe | yes | no | yes |
| Shopify | yes | no | yes |
| Google Sheets | yes | yes | no |

SaaS connectors are subject to the rate limits of the upstream API. Orbital
respects those limits and will slow a run rather than fail it, so a large
first sync against a rate-limited API can take hours. Subsequent incremental
runs are short.

## Streaming

| Connector | Read | Write | Incremental |
|---|---|---|---|
| Apache Kafka | yes | yes | n/a |
| Amazon Kinesis | yes | yes | n/a |

Streaming sources read a bounded window per run rather than running
continuously. Orbital is a scheduled pipeline platform, not a stream
processor; if you need sub-second end-to-end latency, Orbital is the wrong
tool and we would rather say so here than after you have built on it.

## Authentication

Connectors authenticate with API keys, username and password, OAuth 2.0, or
a service account key, depending on what the upstream system offers. Where
OAuth is available, Orbital prefers it, because the resulting credential is
scoped and revocable without changing a shared password.

Credentials are encrypted at rest with a per-workspace key. They are write-
only through the UI and the API: you can replace a credential but never read
one back.

## Custom connectors

A workspace on the Enterprise plan can run a custom connector: a container
image implementing the connector protocol, pulled from your own registry.
Custom connectors run with no network access except to the hosts you
declare, and with no access to other workspaces' credentials.

## Requesting a connector

Connector requests go through your account team. Prioritisation weighs the
number of customers asking, the stability of the upstream API, and whether
the upstream offers an incremental read; a connector that can only ever do
full reads is of limited value and we will say so before building it.
