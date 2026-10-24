# Incident Response Playbook

Owner: Site Reliability, Helix Labs
Audience: every engineer on the Orbital on-call rotation

## Severity levels

Severity is a statement about customer impact, not about how difficult the
problem is to fix.

- Severity 1: Orbital is unavailable, or customer data is at risk of loss or
  disclosure. Page immediately, at any hour.
- Severity 2: A major feature is unusable for many customers, or a single
  large customer is fully blocked. Page during working hours; raise to
  Severity 1 if it is still open after four hours.
- Severity 3: A feature is degraded, or there is a workaround that support
  can give the customer. Handle in the next working day.
- Severity 4: Cosmetic or internal-only. Ticket it; do not page.

When in doubt between two levels, choose the higher one. Downgrading an
incident is cheap. Discovering four hours late that it should have been
Severity 1 is not.

## Roles

Every incident above Severity 3 has three named roles, held by three
different people.

The Incident Commander owns the incident. They do not debug. Their job is to
decide, to delegate and to keep the timeline accurate. The first responder
is Incident Commander by default until they hand the role over explicitly.

The Operations Lead is the only person making changes to production during
the incident. Everyone else proposes; the Operations Lead executes. This is
what prevents two engineers applying contradictory mitigations at the same
time.

The Communications Lead owns the status page and the customer-facing
updates, and posts an update at least every 30 minutes for a Severity 1,
even when the update is "no change yet".

## The first fifteen minutes

1. Acknowledge the page. An unacknowledged page escalates after five
   minutes.
2. Open an incident channel named inc-YYYY-MM-DD-short-description.
3. State the severity and claim the Incident Commander role in the channel.
4. Post the symptom, not the hypothesis. "Checkout API returning 503 for
   roughly 40 percent of requests since 14:12" is a symptom. "The cache is
   broken" is a hypothesis, and stating it early anchors everyone onto it.
5. Decide whether to mitigate or to diagnose. Mitigation wins. Restore
   service first; understand it afterwards.

## Mitigations available without approval

The on-call engineer may take any of these actions without waiting for an
approver:

- Roll back the most recent deployment.
- Disable a feature flag.
- Scale a deployment horizontally, up to three times its baseline replica
  count.
- Shed load from a non-critical background queue.
- Fail a read replica out of the pool.

Anything that deletes data, changes a schema, or modifies customer records
requires a second engineer's explicit agreement, recorded in the incident
channel.

## Communication

The status page is updated within 20 minutes of declaring a Severity 1, and
within one hour for a Severity 2. The first update never contains a cause
and never contains an estimated time to resolution; it contains what is
affected and that we are working on it. An early ETA that slips is worse
than no ETA.

## Closing an incident

An incident is resolved when the customer-visible symptom is gone, not when
the root cause is understood. Mark it resolved, then open the review.

## Post-incident review

Every Severity 1 and Severity 2 gets a written review within five working
days. The review is blameless: it describes what the system did and what
information people had at the time, never who should have known better.

The review must answer four questions:

1. What was the customer impact, in minutes and in affected accounts?
2. What made it hard to detect?
3. What made it hard to diagnose?
4. What single change would have most reduced the impact?

Action items from the review are tickets with owners and dates. A review
that ends in "be more careful" has not produced an action item.
