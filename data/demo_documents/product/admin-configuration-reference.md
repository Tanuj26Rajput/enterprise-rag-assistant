# Orbital Admin Configuration Reference

Product: Orbital 2026.3
Audience: workspace Owners and Admins

Every setting below is found under Settings in the workspace navigation.
Settings marked Owner-only are not visible to Admins.

## Identity and sign-in

### Single sign-on (Owner-only)

Orbital supports SAML 2.0 and OpenID Connect. Configure one or the other,
not both; a workspace with two identity providers has two sources of truth
about who a person is.

After enabling SSO, there is a grace period of 14 days during which password
sign-in continues to work for existing members. This exists so that a
misconfigured identity provider does not lock the whole workspace out. Once
the grace period ends, password sign-in is disabled and only SSO works.

Keep one break-glass Owner account with password sign-in and a hardware
second factor, excluded from SSO enforcement. Store its credentials outside
Orbital.

### Just-in-time provisioning

When enabled, a user who authenticates successfully through SSO and has no
Orbital account gets one, with the role set by the default-role setting.
Set the default role to Viewer. A default of Member means anyone who can
authenticate with your identity provider can create and run pipelines.

### Session lifetime

Access tokens last 30 minutes and are not configurable. Refresh token
lifetime is configurable between 1 and 30 days; the default is 7.

Signing out discards the client's copy of the refresh token. It does not
revoke the token server-side, so a token already captured by an attacker
remains valid until it expires. To cut off a compromised session
immediately, deactivate the member; that invalidates every token issued to
them.

## Network

### IP allow-listing (Owner-only)

Restrict API and console access to a set of CIDR ranges. The list applies to
human sign-in and to API tokens alike.

Add your current address to the list before saving, or you will lock
yourself out. Orbital warns you if your current address is not covered, but
it does not refuse the save; there are legitimate reasons to configure a
list you are not inside.

### Outbound addresses

Connectors make outbound calls from a fixed set of addresses, listed on this
page. Give these to the administrator of any source system that allow-lists
inbound traffic. The set changes rarely and never without 30 days' notice to
workspace Owners.

## Data

### Run history retention

How long run records and their logs are kept. Configurable from 7 to 365
days, subject to your plan's maximum. Reducing the retention period deletes
records older than the new period at the next nightly sweep, and that
deletion cannot be undone.

### Artifact retention

How long pipeline output artifacts are kept in object storage. Independent
of run history retention, because artifacts are usually far larger. Default
is 30 days.

### Audit log

The audit log records every change to pipelines, connections, members and
settings, with the actor, the time, the source address and the request
identifier. It is retained for 400 days on all plans and is not affected by
the run history setting.

Owners can export up to 90 days at a time as newline-delimited JSON.

## Members and roles

### Role defaults

The role assigned to a user created by just-in-time provisioning, and the
role pre-selected in the invitation dialog. Both default to Viewer.

### Deactivating a member

Deactivation removes all access immediately and invalidates every token
issued to that member. Their name remains on the pipelines and runs they
created; Orbital never rewrites history to remove an author.

A deactivated member can be reactivated with their previous role. Deleting a
member is a separate, irreversible action available only to Owners.

## Alerts

### Destinations

Alerts can be delivered by email, by webhook, or to a chat integration. A
webhook destination must respond with a 2xx status within 10 seconds; three
consecutive failures disable the destination and notify the workspace
Owners.

### Rules

Each rule pairs a condition with one or more destinations. Available
conditions are pipeline failure, run duration exceeding a threshold,
schedule skipped because of overlap, connection test failure, and credential
expiring within 14 days.

The credential-expiry alert is the one most workspaces forget to configure
and the one most likely to prevent an incident.

## Billing (Owner-only)

Plan, usage and invoices. Usage is metered on pipeline run-minutes, not on
the number of pipelines. A pipeline that exists but never runs costs
nothing.
