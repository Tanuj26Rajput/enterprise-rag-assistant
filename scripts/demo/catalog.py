"""What the demo data *is*: accounts, workspaces, documents and questions.

Pure data. No database, no network, no side effects - so the catalogue can be
read, reviewed and diffed on its own, and the seeder below it contains only
the mechanics of applying it.

Everything here is fictional. The accounts use addresses in ``example.com``,
which RFC 2606 reserves for exactly this purpose and which can never belong
to a real mailbox. The documents describe a company that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.db.models.enums import NamespaceRole

#: Where the authored sources live, relative to the repository root.
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "data" / "demo_documents"


# --------------------------------------------------------------- accounts ---
@dataclass(frozen=True, slots=True)
class DemoUser:
    """One demo account.

    ``password`` is a plaintext demo password that is hashed by the
    application's own :func:`app.core.security.hash_password` before it
    reaches the database, exactly as a real registration would be. It is in
    source control on purpose: it unlocks nothing but this fictional data,
    and a demo whose credentials are a secret is not a demo.
    """

    email: str
    full_name: str
    password: str
    is_superuser: bool
    description: str


ADMIN = DemoUser(
    email="ada.whitfield@example.com",
    full_name="Ada Whitfield",
    password="Orbital!Demo-Admin-2026",
    # Platform operators resolve to OWNER in every namespace - see
    # NamespaceAuthorizer._resolve_role. This is the only cross-tenant path
    # in the system, and the demo exercises it deliberately.
    is_superuser=True,
    description="Platform administrator. Owns all four demo workspaces.",
)

MEMBER = DemoUser(
    email="marcus.delacroix@example.com",
    full_name="Marcus Delacroix",
    password="Orbital!Demo-Member-2026",
    is_superuser=False,
    description=(
        "Engineer. Admin of Engineering Documentation, Member of the Customer "
        "Support KB. No access at all to HR Policies or Product Documentation."
    ),
)

VIEWER = DemoUser(
    email="priya.raghunathan@example.com",
    full_name="Priya Raghunathan",
    password="Orbital!Demo-Viewer-2026",
    is_superuser=False,
    description=(
        "People Operations. Member of HR Policies, read-only Viewer of Product "
        "Documentation. No access at all to Engineering or Customer Support."
    ),
)

USERS: tuple[DemoUser, ...] = (ADMIN, MEMBER, VIEWER)


# ------------------------------------------------------------- documents ---
@dataclass(frozen=True, slots=True)
class DemoDocument:
    """One source file and the format it is uploaded as.

    The authored text lives in ``data/demo_documents`` as Markdown and is
    rendered to its target format at seed time. Keeping the repository
    text-only means the demo corpus is reviewable in a diff, and rendering
    with the same libraries the ingestion pipeline reads with means the bytes
    are always a valid PDF or DOCX rather than something that merely has the
    right extension.
    """

    source: str
    title: str
    #: ``pdf``, ``docx`` or ``txt``.
    fmt: str

    @property
    def source_path(self) -> Path:
        return SOURCE_ROOT / self.source

    @property
    def filename(self) -> str:
        """The name the document is stored and cited under."""
        return Path(self.source).stem + "." + self.fmt


# ------------------------------------------------------------ workspaces ---
@dataclass(frozen=True, slots=True)
class DemoCollection:
    slug: str
    name: str
    description: str
    documents: tuple[DemoDocument, ...]


@dataclass(frozen=True, slots=True)
class DemoNamespace:
    slug: str
    name: str
    description: str
    collection: DemoCollection
    #: Members other than the administrator, who owns every demo workspace.
    members: tuple[tuple[DemoUser, NamespaceRole], ...] = field(default_factory=tuple)


ENGINEERING = DemoNamespace(
    slug="demo-engineering",
    name="Engineering Documentation",
    description="Runbooks, playbooks and reference material for the Orbital platform.",
    members=((MEMBER, NamespaceRole.ADMIN),),
    collection=DemoCollection(
        slug="runbooks",
        name="Runbooks and references",
        description="Operational documentation for engineers on the Orbital rotation.",
        documents=(
            DemoDocument(
                "engineering/deployment-runbook.md",
                "Orbital Deployment Runbook",
                "pdf",
            ),
            DemoDocument(
                "engineering/incident-response-playbook.md",
                "Incident Response Playbook",
                "txt",
            ),
            DemoDocument(
                "engineering/service-architecture-overview.md",
                "Orbital Service Architecture Overview",
                "docx",
            ),
            DemoDocument(
                "engineering/api-error-reference.md",
                "Orbital API Error Reference",
                "txt",
            ),
            DemoDocument(
                "engineering/information-security-policy.md",
                "Information Security Policy",
                "pdf",
            ),
        ),
    ),
)

HR = DemoNamespace(
    slug="demo-hr",
    name="HR Policies",
    description="Company-wide people policies for Helix Labs.",
    members=((VIEWER, NamespaceRole.MEMBER),),
    collection=DemoCollection(
        slug="policies",
        name="People policies",
        description="Handbook, leave, remote work and conduct.",
        documents=(
            DemoDocument("hr/employee-handbook.md", "Helix Labs Employee Handbook", "pdf"),
            DemoDocument("hr/leave-policy.md", "Leave Policy", "docx"),
            DemoDocument("hr/remote-work-policy.md", "Remote Work Policy", "txt"),
            DemoDocument("hr/code-of-conduct.md", "Code of Conduct", "pdf"),
        ),
    ),
)

PRODUCT = DemoNamespace(
    slug="demo-product",
    name="Product Documentation",
    description="Customer-facing documentation for the Orbital product.",
    members=((VIEWER, NamespaceRole.VIEWER),),
    collection=DemoCollection(
        slug="product-docs",
        name="Orbital product docs",
        description="Setup, configuration, integrations and release notes.",
        documents=(
            DemoDocument("product/orbital-setup-guide.md", "Orbital Setup Guide", "pdf"),
            DemoDocument(
                "product/release-notes-2026-q3.md",
                "Orbital Release Notes, Q3 2026",
                "txt",
            ),
            DemoDocument(
                "product/admin-configuration-reference.md",
                "Orbital Admin Configuration Reference",
                "docx",
            ),
            DemoDocument(
                "product/integrations-overview.md",
                "Orbital Integrations Overview",
                "txt",
            ),
        ),
    ),
)

SUPPORT = DemoNamespace(
    slug="demo-support",
    name="Customer Support KB",
    description="The knowledge base support engineers answer tickets from.",
    members=((MEMBER, NamespaceRole.MEMBER),),
    collection=DemoCollection(
        slug="knowledge-base",
        name="Support knowledge base",
        description="Troubleshooting, FAQs and escalation rules.",
        documents=(
            DemoDocument(
                "support/api-troubleshooting-guide.md",
                "API Troubleshooting Guide",
                "pdf",
            ),
            DemoDocument("support/customer-faq.md", "Orbital Customer FAQ", "txt"),
            DemoDocument(
                "support/billing-and-plans-faq.md",
                "Billing and Plans FAQ",
                "docx",
            ),
            DemoDocument("support/escalation-matrix.md", "Support Escalation Matrix", "txt"),
        ),
    ),
)

NAMESPACES: tuple[DemoNamespace, ...] = (ENGINEERING, HR, PRODUCT, SUPPORT)


# ------------------------------------------------------------ scenarios ---
@dataclass(frozen=True, slots=True)
class DemoScenario:
    """A seeded conversation: who asks, where, and what.

    The answers are *not* here, and deliberately so. A seeded conversation is
    produced by running the real retrieval and generation path, so its answer
    and its citations come from the documents that were actually retrieved.
    Writing an expected answer into this file would make the demo a
    recording rather than a demonstration.
    """

    namespace: DemoNamespace
    asked_by: DemoUser
    title: str
    questions: tuple[str, ...]


SCENARIOS: tuple[DemoScenario, ...] = (
    DemoScenario(
        namespace=HR,
        asked_by=VIEWER,
        title="Leave entitlement and carry-over",
        questions=(
            "How many days of annual leave do full-time employees get, "
            "and how many can be carried over?",
            "When do carried-over days expire?",
        ),
    ),
    DemoScenario(
        namespace=ENGINEERING,
        asked_by=MEMBER,
        title="Deployment rollback criteria",
        questions=(
            "Under what conditions should a deployment be rolled back immediately?",
            "Are database migrations rolled back too?",
        ),
    ),
    DemoScenario(
        namespace=SUPPORT,
        asked_by=MEMBER,
        title="Uploads rejected as duplicates",
        questions=(
            "A customer says their upload was rejected as a duplicate even though "
            "they renamed the file. Why?",
        ),
    ),
    DemoScenario(
        namespace=PRODUCT,
        asked_by=ADMIN,
        title="Single sign-on rollout",
        questions=(
            "What happens to password sign-in after we enable single sign-on?",
        ),
    ),
)
