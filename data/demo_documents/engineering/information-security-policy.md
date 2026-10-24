# Information Security Policy

Owner: Security Engineering, Helix Labs
Approved by: the Helix Labs Security Council
Effective: 1 July 2026
Review cycle: annual, or after any Severity 1 security incident

## 1. Purpose and scope

This policy states the security rules that apply to everyone who builds,
operates or supports Orbital, and to every system that stores or processes
customer data. It applies to employees, contractors and anyone with access
to a Helix Labs production system.

## 2. Access control

Access is granted on the principle of least privilege. A person receives the
narrowest access that lets them do their current job, and that access is
reviewed when their job changes, not only when they leave.

Multi-factor authentication is mandatory for every account that can reach a
production system. Hardware security keys are issued to all engineers; a
time-based one-time password is acceptable only as a backup factor and never
as a sole second factor for production access.

Shared accounts are prohibited. Where a system genuinely cannot support
individual accounts, the exception is registered with Security Engineering,
the credential is stored in the managed secret store, and every use is
logged against a named person.

Production access is time-bounded. Standing production access is granted
only to the on-call rotation and to Security Engineering. Everyone else
requests elevated access for a stated reason; the grant expires after eight
hours and is logged.

## 3. Secrets

Secrets never appear in source control, in log output, in ticket
descriptions or in chat messages. This includes API keys, database
passwords, signing keys and customer credentials for connected systems.

All secrets live in the managed secret store and are delivered to workloads
as environment variables at start-up. Rotation is quarterly by default, and
immediate whenever a secret has been exposed or a person with access to it
leaves the company.

If a secret is exposed, the exposure is treated as a Severity 2 incident at
minimum. Rotate first, investigate afterwards. A secret that was committed
and then removed in a later commit is still exposed; the history retains it.

## 4. Data classification

Four classifications, with handling rules attached to each.

- **Public**: published material. No restriction.
- **Internal**: ordinary business information. Accessible to all employees;
  not shared outside the company without approval.
- **Confidential**: customer data, financial records, personal data of
  employees. Access on a need-to-know basis, encrypted at rest and in
  transit, never copied to a personal device.
- **Restricted**: credentials, signing keys, security findings before they
  are fixed. Access individually approved and logged.

Customer data is Confidential by default. Any export of customer data to a
non-production environment requires written approval and must be
anonymised; production data is never restored into a development
environment.

## 5. Devices

Company laptops have full-disk encryption enabled and a screen lock of no
more than five minutes. Operating system and browser updates are applied
within 14 days of release, and within 48 hours for a vulnerability rated
critical.

Personal devices may access email and chat through the managed application
profile. They may not access production systems or store customer data.

## 6. Vulnerability management

Dependencies are scanned on every build. Findings are remediated on this
schedule, measured from the date the finding is first reported:

- Critical: 7 days
- High: 30 days
- Medium: 90 days
- Low: next scheduled maintenance

A finding that cannot be remediated within its window requires a documented
risk acceptance signed by the owning team's lead and by Security
Engineering. Risk acceptances expire after 90 days and must be renewed
explicitly; an expired acceptance is a finding again.

## 7. Reporting a security concern

Anyone who suspects a security problem reports it to Security Engineering
immediately, through the security channel or by email to the security
address. Report suspicion, not certainty: a report that turns out to be
nothing costs twenty minutes, and an unreported concern that turns out to be
something costs considerably more.

There is no penalty for reporting a problem you caused. There is a penalty
for concealing one.

## 8. Compliance

Violation of this policy may result in withdrawal of access and, for serious
or repeated violations, disciplinary action under the terms set out in the
employee handbook.
