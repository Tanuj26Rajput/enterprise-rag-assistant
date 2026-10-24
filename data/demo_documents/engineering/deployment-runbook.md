# Orbital Deployment Runbook

Owner: Platform Engineering, Helix Labs
Review cycle: every 90 days
Last reviewed: 12 August 2026

## 1. Scope

This runbook covers routine deployments of the Orbital control plane to the
production cluster prod-eu-1. It does not cover database migrations that
drop columns, region failover, or the first deployment of a brand new
service; each of those has its own change record and requires a named
approver from the Platform Engineering leadership group.

## 2. Deployment windows

Routine deployments run Tuesday to Thursday, between 09:00 and 15:00 CET.
Monday is reserved for the weekend backlog and Friday deployments are not
permitted without an approved exception, because the on-call rotation hands
over at 17:00 on Friday and a regression discovered after that point has no
author available until Monday.

Emergency fixes may deploy at any time. An emergency fix is defined as a
change that resolves an active Severity 1 or Severity 2 incident. Nothing
else qualifies, including a change that is merely urgent to a stakeholder.

## 3. Pre-flight checklist

Before promoting a build, the deploying engineer confirms all of the
following:

- The change has at least one approving review from a code owner.
- The staging deployment of the same commit has been live for 30 minutes
  with no new error-budget burn.
- Any schema migration in the change is additive. Additive means new tables,
  new nullable columns, and new indexes created concurrently. Dropping a
  column is a separate change, deployed at least one release after the code
  that stopped reading it.
- The feature flag defaults for the change are set to off.
- The rollback target, meaning the previous known-good image tag, is written
  in the deployment ticket before the deployment begins, not after.

## 4. Deployment procedure

1. Announce the deployment in the platform-deploys channel with the commit
   SHA, the ticket link and the rollback tag.
2. Promote the image from the staging registry. Promotion is a tag copy; the
   image is never rebuilt between staging and production, because a rebuild
   produces a different artifact from the one that was tested.
3. Apply pending migrations from the migration job, and wait for it to
   report success. The application never creates or alters tables itself.
4. Roll the control-plane deployment. The rollout is configured with
   maxSurge 1 and maxUnavailable 0, so capacity never drops during a
   deployment.
5. Watch the four golden signals dashboard for 15 minutes: request rate,
   error rate, p99 latency and saturation.

## 5. Verification

A deployment is considered verified when, 15 minutes after the rollout
completes, all of the following hold:

- The HTTP 5xx rate is below 0.1 percent of total requests.
- The p99 latency of the pipeline run endpoint is below 800 ms.
- The ingestion job queue depth is not growing monotonically.
- No new entries have appeared in the dead-letter job table.

Record the verification result in the deployment ticket. An unverified
deployment is treated as a failed deployment at the next review.

## 6. Rollback

Roll back immediately, without further analysis, if the error rate exceeds
1 percent for two consecutive minutes or if p99 latency doubles against the
pre-deployment baseline. Diagnosis happens after service is restored, not
before.

To roll back, re-deploy the rollback tag recorded in the pre-flight
checklist. Do not roll back migrations. Additive migrations are safe to
leave in place, which is precisely why the additive rule exists: it means a
code rollback is always possible without a database rollback.

If a rollback does not restore service within 10 minutes, declare an
incident and page the on-call lead.

## 7. After the deployment

Close the deployment ticket with the verification result and the final image
tag. If the deployment was rolled back, open a follow-up ticket before the
end of the working day; a rollback with no follow-up ticket is how the same
regression ships twice.
