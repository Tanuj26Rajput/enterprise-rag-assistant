# Demo scenarios

Questions the seeded corpus can answer, grouped by workspace, with the
document each answer should come from.

> **The answers are not written down here, and that is the point.**
>
> Every answer this system produces is generated from passages it actually
> retrieved, and every citation is resolved by the application against the
> passages it supplied — never by the model. Publishing an expected answer
> and an expected citation in this file would turn a demonstration into a
> recording, and would be exactly the failure the design exists to prevent.
>
> What is listed below is therefore the question and the source it is drawn
> from. Run it and read what comes back.

Seed the data first:

```bash
python -m scripts.seed_demo_data --yes
```

Sign in with an account from [docs/demo-credentials.md](demo-credentials.md).

---

## HR Policies (`demo-hr`)

Sign in as **Priya Raghunathan** (Member) or **Ada Whitfield** (admin).

| Ask | Answered from |
|---|---|
| How many days of annual leave do full-time employees get, and how many can be carried over? | `leave-policy.docx` |
| When do carried-over leave days expire? | `leave-policy.docx` |
| When is a medical certificate required for sick leave? | `leave-policy.docx` |
| How much parental leave is there, and can it be split up? | `leave-policy.docx` |
| How long is the probation period and what is the notice period during it? | `employee-handbook.pdf` |
| What is the annual learning budget? | `employee-handbook.pdf` |
| How do I raise a concern about a colleague's behaviour, and what happens next? | `code-of-conduct.pdf` |
| Can I work from another country, and for how long? | `remote-work-policy.txt` |
| What are core hours? | `employee-handbook.pdf`, `remote-work-policy.txt` |

The last one is worth running: two documents say the same thing, and the
answer should cite both rather than picking one.

---

## Engineering Documentation (`demo-engineering`)

Sign in as **Marcus Delacroix** (Admin) or **Ada Whitfield**.

| Ask | Answered from |
|---|---|
| Under what conditions should a deployment be rolled back immediately? | `deployment-runbook.pdf` |
| Are database migrations rolled back during a rollback? | `deployment-runbook.pdf` |
| What has to be in the deployment ticket before the deployment starts? | `deployment-runbook.pdf` |
| What does the Incident Commander do, and what do they not do? | `incident-response-playbook.txt` |
| What can the on-call engineer do without asking anyone's approval? | `incident-response-playbook.txt` |
| Why is the job queue a database table rather than a message broker? | `service-architecture-overview.docx` |
| How quickly must a critical vulnerability be remediated? | `information-security-policy.pdf` |
| What should I do if an API key is committed to a repository? | `information-security-policy.pdf` |
| Why does the API return 404 instead of 403 for a workspace I'm not in? | `api-error-reference.txt` |

---

## Product Documentation (`demo-product`)

Sign in as **Priya Raghunathan** (Viewer — she can ask questions but cannot
upload) or **Ada Whitfield**.

| Ask | Answered from |
|---|---|
| What happens to password sign-in after we enable single sign-on? | `admin-configuration-reference.docx` |
| What should the default role for just-in-time provisioned users be, and why? | `admin-configuration-reference.docx` |
| How do I stop a compromised session immediately? | `admin-configuration-reference.docx` |
| What are the four workspace roles and what can each one do? | `orbital-setup-guide.pdf` |
| What time zone do schedules run in? | `orbital-setup-guide.pdf`, `customer-faq.txt` |
| Which connectors support incremental reads? | `integrations-overview.txt` |
| Why can't I use an Excel file as a source? | `integrations-overview.txt` |
| What changed about the runs endpoint in 2026.3.2? | `release-notes-2026-q3.txt` |

---

## Customer Support KB (`demo-support`)

Sign in as **Marcus Delacroix** (Member) or **Ada Whitfield**.

| Ask | Answered from |
|---|---|
| A customer renamed their file and the upload was still rejected as a duplicate. Why? | `api-troubleshooting-guide.pdf`, `customer-faq.txt` |
| A PDF uploaded fine but no text was found. What do I tell the customer? | `api-troubleshooting-guide.pdf` |
| How long do I have before a Severity 2 ticket must leave Tier 1? | `escalation-matrix.txt` |
| What must be in a ticket before I escalate it? | `escalation-matrix.txt` |
| What happens if a customer's card payment fails? | `billing-and-plans-faq.docx` |
| Is a downgrade immediate? | `billing-and-plans-faq.docx` |
| How is usage measured — per pipeline or per minute? | `billing-and-plans-faq.docx`, `customer-faq.txt` |

---

## Questions that should be refused

These have no answer in the corpus. A correct response says so and cites
nothing. If any of them produces a confident answer, something is wrong.

- What is the company's share price?
- Who is the CEO of Helix Labs?
- What is the admin database password?
- How many employees does the company have?
- What is the roadmap for 2027?

The last one is the most interesting: the corpus contains release notes and
a configuration reference, so retrieval will find superficially related
passages. The answer should still be that the documents do not say.

---

## Cross-tenant checks

These demonstrate isolation rather than retrieval quality.

| Signed in as | Ask, in | Expected |
|---|---|---|
| Marcus | Customer Support KB: "How much annual leave do we get?" | No answer. The HR documents are in a workspace he cannot read. |
| Priya | Product Documentation: try to upload a file | Refused. She is a Viewer there. |
| Priya | HR Policies: upload a file | Succeeds. She is a Member there. |
| Marcus | open `demo-hr` by its id | 404, not 403. |

---

## Seeded conversations

`--with-conversations` runs four of the questions above as real turns and
stores the threads, so the UI has conversation history on first open:

```bash
python -m scripts.seed_demo_data --yes --with-conversations
```

This is opt-in because it is the only part of seeding that calls a chat
model. It sends six chat completions to whatever `LLM_PROVIDER` and
`LLM_MODEL` are configured, and costs whatever that provider charges for
them. Without the flag, no chat model is called at any point.

The threads it creates are:

| Workspace | Thread | Asked by |
|---|---|---|
| HR Policies | Leave entitlement and carry-over | Priya |
| Engineering Documentation | Deployment rollback criteria | Marcus |
| Customer Support KB | Uploads rejected as duplicates | Marcus |
| Product Documentation | Single sign-on rollout | Ada |

Their answers and citations are produced by the live retrieval and
generation path at seed time. Nothing about them is written down in advance.
