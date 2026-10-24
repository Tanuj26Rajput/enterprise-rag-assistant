# Support Escalation Matrix

Owner: Customer Support, Helix Labs
Effective: 1 September 2026

This document says who handles what, how long each stage has, and what
triggers a move to the next stage. It exists so that nobody has to decide
during a stressful ticket whether they are allowed to escalate.

## Tiers

### Tier 1: Support Engineer

Handles every incoming ticket. Resolves anything covered by the API
Troubleshooting Guide or the Customer FAQ: authentication errors,
unsupported file types, duplicate uploads, scheduling questions, billing
questions, and how-to questions.

Tier 1 does not have production database access and does not make changes to
a customer's workspace.

### Tier 2: Senior Support Engineer

Handles tickets that need investigation: reading application logs by request
identifier, reproducing a reported behaviour, and reading the audit log for
an account.

Tier 2 has read-only production access, scoped to the account named in the
ticket, and may make changes within a customer workspace with the customer's
written agreement in the ticket.

### Tier 3: Platform Engineering

Handles defects: anything where Orbital is doing something it should not,
and anything requiring a code change or a data correction.

Tier 3 is reached through the on-call rotation, never by messaging an
individual engineer directly. Messaging an individual produces an answer
that is not recorded anywhere and does not survive that person going on
holiday.

## Time limits

A ticket moves to the next tier when its limit is reached, whether or not
anyone has asked for it to be escalated. The limits are measured in working
hours for Severity 3 and 4, and in wall-clock hours for Severity 1 and 2.

| Severity | Tier 1 limit | Tier 2 limit | First response target |
|---|---|---|---|
| 1 | 30 minutes | 1 hour | 1 hour, Enterprise; 4 hours otherwise |
| 2 | 2 hours | 4 hours | 4 hours |
| 3 | 1 working day | 3 working days | 1 working day |
| 4 | 3 working days | n/a | 3 working days |

## Immediate escalation

Skip the time limits and escalate at once for any of these, regardless of
tier:

- Suspected data loss or data corruption.
- Suspected exposure of one customer's data to another.
- Any report that looks like a security vulnerability. These also go to
  Security Engineering immediately, in parallel.
- A 500 error reproducible on demand.
- The same symptom reported by three or more accounts within one hour.
- Anything affecting an Enterprise account during an announced critical
  period, such as a migration window.

The third item bears repeating: a customer reporting a security problem is
not a support ticket that happens to be about security. Treat it as a
security incident from the first message, and do not discuss the details in
the ticket thread beyond acknowledging it.

## How to escalate

Escalation is a handover, not a forward. Before escalating, the ticket must
contain:

1. The customer's workspace identifier.
2. At least one request identifier for the failing call, where the problem
   is an API error.
3. The time the problem occurred, with the time zone.
4. What the customer expected and what happened instead.
5. What you have already checked and ruled out.

Item 5 is the one most often skipped and the one that most often causes the
next tier to repeat work that has already been done.

## Customer communication during escalation

The customer stays informed by the person who received the ticket, not by
whoever is currently working on it. One voice to the customer.

Update the customer when the ticket is escalated, and then at least once per
time limit period, even when the update is that there is no news. Silence is
read as being ignored.

Never tell a customer an estimated fix time that you have not been given by
the tier that would do the fixing.

## Deescalation and closing

A ticket is closed when the customer agrees it is resolved, or after two
follow-up attempts over five working days with no reply.

A ticket resolved by a workaround is not closed; it stays open at Tier 3,
linked to the defect ticket, until the underlying problem is fixed. The
customer is told which of the two they are waiting on.

## Reviewing this matrix

Customer Support reviews these limits quarterly against the actual
distribution of ticket resolution times. If a limit is being missed more
than 10 percent of the time, either the limit or the staffing is wrong, and
the review says which.
