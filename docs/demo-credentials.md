# Demo credentials

> **Development and demonstration only.**
>
> These are fictional accounts holding fictional data. The passwords below
> are in source control on purpose: they unlock nothing but the demo
> dataset, on a database you seeded yourself.
>
> They must never be created on a production system. The seed script refuses
> to run when `ENVIRONMENT=production`, and there is no flag to override it.
>
> No real secret — no API key, no database password, no token — appears in
> this file or anywhere else in the demo data.

Created by:

```bash
python -m scripts.seed_demo_data --yes
```

## Accounts

| Name | Email | Password | Platform role |
|---|---|---|---|
| Ada Whitfield | `ada.whitfield@example.com` | `Orbital!Demo-Admin-2026` | Administrator (superuser) |
| Marcus Delacroix | `marcus.delacroix@example.com` | `Orbital!Demo-Member-2026` | Regular user |
| Priya Raghunathan | `priya.raghunathan@example.com` | `Orbital!Demo-Viewer-2026` | Regular user |

The addresses are in `example.com`, which [RFC 2606][rfc2606] reserves for
documentation. They can never route to a real mailbox.

Passwords are hashed with Argon2 by `app.core.security.hash_password` — the
same function the registration endpoint calls — before they reach the
database. The seeder has no path that writes a password hash itself, and
authentication is not weakened or bypassed anywhere for the demo.

[rfc2606]: https://www.rfc-editor.org/rfc/rfc2606

## Who can see what

This is the part worth demonstrating. Each regular user can reach exactly two
of the four workspaces, with a different role in each.

| Workspace | Ada (admin) | Marcus | Priya |
|---|---|---|---|
| Engineering Documentation (`demo-engineering`) | Owner | **Admin** | *no access* |
| HR Policies (`demo-hr`) | Owner | *no access* | **Member** |
| Product Documentation (`demo-product`) | Owner | *no access* | **Viewer** |
| Customer Support KB (`demo-support`) | Owner | **Member** | *no access* |

Ada reaches all four because `is_superuser` resolves to `OWNER` in every
namespace — the single deliberate cross-tenant path in the system, meant for
support and incident response (`app/services/authorization.py`).

### What the roles mean

| Role | Read | Upload / edit | Manage members | Delete the workspace |
|---|---|---|---|---|
| Owner | yes | yes | yes | yes |
| Admin | yes | yes | yes | no |
| Member | yes | yes | no | no |
| Viewer | yes | no | no | no |

### Isolation worth checking by hand

- Sign in as **Marcus** and open HR Policies by its id. It returns **404,
  not 403** — non-membership is indistinguishable from non-existence, so
  namespace ids cannot be enumerated.
- Sign in as **Priya** and try to upload into Product Documentation. She is a
  Viewer there; the upload is refused. The same account uploading into HR
  Policies, where she is a Member, succeeds.
- Ask a question as **Marcus** in the Customer Support KB about leave
  carry-over. The HR documents that answer it are in a workspace he cannot
  read, so retrieval finds nothing and the assistant says it does not know —
  rather than answering from a document he has no right to see.

## Rotating or removing the demo accounts

The seeder never deletes anything, so there is no `--reset`. To remove the
demo data from a development database, delete the four `demo-*` namespaces
and the three accounts through the API or by hand.

If you have already run the seeder and want different passwords, change them
in `scripts/demo/catalog.py` and then change them for the existing accounts
through the application. Re-running the seeder will **not** reset the
password of an account that already exists: silently changing the
credentials of an account you did not create would be a destructive act, and
the seeder has no destructive acts.

## Related

- [docs/demo-scenarios.md](demo-scenarios.md) — questions to ask, and the
  documents that answer them.
- [data/demo_documents/](../data/demo_documents/) — the authored sources.
