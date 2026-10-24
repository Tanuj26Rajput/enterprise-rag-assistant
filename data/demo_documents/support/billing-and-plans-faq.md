# Billing and Plans FAQ

Owner: Customer Support, Helix Labs
Last updated: 18 September 2026

## The plans

### Starter

For an individual or a small team evaluating Orbital.

- One workspace, up to 5 members
- 2,000 included run-minutes per month
- 30 days of run history
- Community support
- No single sign-on, no IP allow-listing, no custom connectors

### Standard

The plan most teams run on.

- Unlimited workspaces and members
- 20,000 included run-minutes per month
- 90 days of run history, configurable from 7 to 90
- Email support, Monday to Friday, first response within one working day
- Single sign-on included

### Enterprise

- Unlimited workspaces and members
- Run-minutes negotiated in the contract
- Up to 365 days of run history
- 24-hour support for Severity 1, first response within one hour
- IP allow-listing, custom connectors, audit log export, a named account
  team, and an annual invoice with purchase-order support

## How usage is measured

Usage is pipeline run-minutes: the wall-clock time each pipeline run spends
executing, summed across all pipelines in all workspaces on the account,
rounded up to the nearest minute per run.

Three things that are commonly assumed to cost money and do not:

- The number of pipelines, connections, workspaces or members.
- Storage of documents and artifacts within the retention period.
- A pipeline that is defined but never runs.

Dry runs are metered at the same rate as real runs, because they perform
every read and every transform; only the write is skipped.

## Overages

If you pass your included run-minutes, runs continue. The excess is billed
on the next invoice at the overage rate in your plan.

Workspace Owners receive an alert at 80 percent and again at 100 percent of
included usage. Nothing is throttled or stopped without notice at any
point.

## Changing plan

### Upgrading

Takes effect immediately. You are charged the difference for the remainder
of the current billing period, pro-rated by day.

### Downgrading

Takes effect at the start of the next billing period, not immediately. This
gives you time to come within the lower plan's limits.

Before a downgrade takes effect, check: workspace count against the Starter
limit, run history retention against the new plan's maximum, and whether you
are relying on single sign-on. Run history beyond the new maximum is deleted
at the first nightly sweep after the downgrade, and that cannot be undone.

## Invoices and payment

Invoices are issued on the first day of the billing period and are payable
within 14 days for card payments and 30 days for bank transfer. Enterprise
accounts may be invoiced annually in advance.

Invoices are available to workspace Owners under Settings, then Billing.
They are issued in EUR, USD or GBP; a currency change takes effect from the
next billing period.

VAT is applied according to your billing address and VAT registration
status. Add your VAT number under Billing before the invoice is issued;
we cannot re-issue a past invoice to add one.

### A payment failed

We retry a failed card payment three times over seven days and email the
billing contact each time. If payment has not succeeded after that, the
account moves to read-only: pipelines stop running, and data is retained in
full. Nothing is deleted for 60 days.

## Cancellation

Cancel under Settings, then Billing. Cancellation takes effect at the end of
the current billing period; we do not pro-rate refunds for an unused part of
a period.

After cancellation the account is read-only for 30 days, during which you
can export pipeline definitions, run history and artifacts. After 30 days
the data is deleted and cannot be recovered.

## Trials

A 14-day trial of Standard is available to new accounts and does not require
a card. At the end of the trial the account moves to Starter automatically
unless a plan is chosen, so nothing is interrupted by surprise.

## Who to ask

Billing questions go to the billing address and are answered within one
working day. Questions about contract terms or a quote go to your account
team.
