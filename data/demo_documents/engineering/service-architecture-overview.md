# Orbital Service Architecture Overview

Owner: Platform Engineering, Helix Labs
Status: current as of the 2026.3 release

## Purpose

This document describes how the Orbital platform is put together, so that an
engineer joining a team can reason about where a change belongs before
reading any code. It is deliberately a map, not a specification: where it
disagrees with the code, the code is correct and this document is a bug.

## The four services

Orbital is four deployable services behind one gateway.

**The gateway** terminates TLS, authenticates the request, and routes it.
It holds no business logic and no database connection. Its only decisions
are "is this token valid" and "which service handles this path".

**The control plane** is the API that customers and the web console talk to.
It owns pipelines, schedules, connections and permissions. Every write goes
through it, and it is the only service with write access to the primary
database.

**The execution plane** runs pipeline tasks. It claims work from a queue
table in PostgreSQL using SELECT FOR UPDATE SKIP LOCKED and holds a lease
while it works. It can read the primary database and write only to the task
result tables.

**The connector service** holds the integrations with third-party systems.
It is isolated because connector code is the least trusted code in the
platform: it parses data we did not produce, and a crash there must not take
the control plane with it.

## Why the queue is a database table

Orbital does not run a message broker. Task state and the queue entry commit
in the same PostgreSQL transaction, which makes the state "the task
succeeded but the pipeline was never marked complete" unreachable. With a
separate broker that state is reachable on every network partition, and
every team that has one eventually writes a reconciler to repair it.

The cost is real and accepted: throughput is bounded by what a single
PostgreSQL primary can sustain, currently measured at roughly 4,000 claims
per second with the lease pattern. Above that we would need to shard the
queue table by tenant before reaching for a broker.

## Data stores

- **PostgreSQL 16** holds all relational state. One primary, two read
  replicas. Replicas serve the console's list endpoints only; anything that
  reads its own write goes to the primary.
- **Object storage** holds pipeline artifacts and uploaded files. Objects
  are immutable once written; a new version is a new key.
- **Redis** is a cache only. Nothing in Orbital is correct only because
  Redis is up. Losing the entire cache degrades latency and nothing else.

## Tenancy

Every row that belongs to a customer carries a tenant identifier, and every
query is filtered on it in application code. This is enforced at the
repository layer: a repository method that does not take a tenant
identifier does not exist.

Row-level security in PostgreSQL would be stronger, because it would hold
even when application code is wrong. It is on the roadmap and is not yet
implemented, which means a missing filter in a repository is a cross-tenant
bug rather than a query that simply returns nothing. Reviews of repository
code should weigh that accordingly.

## Request lifecycle

A write request passes through five stages, in order: authenticate at the
gateway, authorize against the tenant in the control plane, validate the
payload, apply the change in one transaction, and emit an audit record. The
audit record is written inside the same transaction as the change, so an
audited action that did not happen and an action that happened without an
audit record are both impossible.

## Observability

Every request carries a request identifier generated at the gateway and
propagated through every downstream call, every log line and every audit
row. One identifier joins the customer's HTTP response to the logs and to
the audit trail, which is the single most useful property the platform has
during an incident.

Metrics are Prometheus exposition, scraped every 15 seconds. Liveness and
readiness are separate endpoints: liveness consults nothing, readiness
checks the database and object storage. Using readiness for liveness turns
a database failover into a fleet-wide restart loop.

## Known weak points

- The connector service has no per-connector resource isolation. One
  pathological connector can exhaust the pool for all of them.
- There is no rate limiting inside the platform; it is provided by the edge.
- The execution plane's lease duration is a single global constant, which
  means a long task and a short task get the same recovery latency.
