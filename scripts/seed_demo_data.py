"""Seed the demo dataset: three accounts, four workspaces, seventeen documents.

Run deliberately, from the repository root, against a development database:

    python -m scripts.seed_demo_data --yes

Nothing in the application imports this module. It is never run at startup, it
is never run by a migration, and it never deletes or resets anything: every
step checks for what it would create and skips it if it is already there, so
running it twice is the same as running it once.

The schema must already exist - run ``alembic upgrade head`` first. This
script creates no tables, for the same reason the application does not.

By default it seeds accounts, workspaces and documents, which costs one
embedding call per batch of chunks and nothing else. ``--with-conversations``
additionally runs real question-and-answer turns through the configured chat
model, which costs chat tokens; see the note printed by ``--help``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Allow `python scripts/seed_demo_data.py` as well as `python -m
# scripts.seed_demo_data`; the former puts scripts/ on the path, not the
# repository root, and every import below is absolute from the root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engine  # noqa: E402
from scripts.demo import catalog  # noqa: E402
from scripts.demo.seeder import DemoSeeder, Report  # noqa: E402

#: Set this to ``1`` to authorise seeding without passing ``--yes``.
CONFIRM_ENV_VAR = "SEED_DEMO_DATA"

logger = logging.getLogger("seed")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_demo_data",
        description="Seed fictional demo data for local development and demos.",
        epilog=(
            "Costs: documents are embedded through the configured embedding "
            "provider (EMBEDDING_PROVIDER). With EMBEDDING_PROVIDER=hash there "
            "is no network call and no cost at all. With openai or openrouter, "
            "seeding embeds roughly 17 documents' worth of chunks, once. "
            "--with-conversations additionally sends 6 chat completions to "
            "LLM_PROVIDER. Nothing else in this script reaches the network."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"Confirm the write. Equivalent to setting {CONFIRM_ENV_VAR}=1.",
    )
    parser.add_argument(
        "--with-conversations",
        action="store_true",
        help=(
            "Also seed example conversations by running real retrieval and "
            "generation. Requires a working chat provider and spends tokens."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log at DEBUG, including the application's own loggers.",
    )
    return parser.parse_args(argv)


def _describe_target() -> str:
    """Where this run would write, with the password removed.

    Printed so nobody seeds a database they did not mean to. Rendered through
    SQLAlchemy's own URL type, which masks the password; the raw URL is never
    printed, logged or included in the summary.
    """
    from sqlalchemy.engine import make_url

    settings = get_settings()
    try:
        url = make_url(settings.async_database_url)
    except Exception:  # noqa: BLE001 - a malformed URL is the engine's error to raise
        return "(unparseable DATABASE_URL)"
    return f"{url.host or 'localhost'}/{url.database or '?'}"


def _confirmed(args: argparse.Namespace) -> bool:
    return args.yes or os.environ.get(CONFIRM_ENV_VAR, "").strip() in {"1", "true", "yes"}


def _print_summary(report: Report, *, with_conversations: bool) -> None:
    def section(heading: str, created: list[str], existing: list[str]) -> None:
        print(f"\n{heading}: {len(created)} created, {len(existing)} already present")
        for item in created:
            print(f"  + {item}")
        for item in existing:
            print(f"  = {item}")

    print("\n" + "=" * 68)
    print("Demo data")
    print("=" * 68)
    section("Accounts", report.users_created, report.users_existing)
    section("Workspaces", report.namespaces_created, report.namespaces_existing)
    section("Memberships", report.memberships_created, report.memberships_existing)
    section("Collections", report.collections_created, report.collections_existing)
    section("Documents", report.documents_created, report.documents_existing)
    if with_conversations:
        section("Conversations", report.conversations_created, report.conversations_existing)

    if report.documents_failed:
        print(f"\nDocuments that failed ({len(report.documents_failed)}):")
        for item in report.documents_failed:
            print(f"  ! {item}")
    if report.conversations_failed:
        print(f"\nConversations that failed ({len(report.conversations_failed)}):")
        for item in report.conversations_failed:
            print(f"  ! {item}")

    print("\nSign in at the UI with any of:")
    for demo in catalog.USERS:
        print(f"  {demo.email}  /  {demo.password}")
    print("\nThese are development-only credentials for fictional accounts.")
    print("Documented in docs/demo-credentials.md. Never use them anywhere real.")


async def _seed(args: argparse.Namespace) -> int:
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        seeder = DemoSeeder(session, settings)
        try:
            users = await seeder.seed_users()
            await seeder.seed_namespaces(users)
            if args.with_conversations:
                await seeder.seed_conversations(users)
        except Exception:
            # Each step commits its own unit of work, so what has already
            # succeeded stays. This rollback only discards the partial unit
            # that was in flight when the failure happened.
            await session.rollback()
            logger.exception("Seeding stopped early")
            _print_summary(seeder.report, with_conversations=args.with_conversations)
            return 1

    _print_summary(seeder.report, with_conversations=args.with_conversations)
    return 1 if seeder.report.anything_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s  %(message)s",
    )
    if not args.verbose:
        # The application's own loggers are chatty at INFO during ingestion.
        logging.getLogger("app").setLevel(logging.WARNING)

    settings = get_settings()

    if settings.is_production:
        print(
            "Refusing to seed: ENVIRONMENT=production.\n"
            "This script writes fictional accounts with published passwords. "
            "There is no flag to override this.",
            file=sys.stderr,
        )
        return 2

    target = _describe_target()

    if not _confirmed(args):
        print(
            f"About to seed demo data into {target}.\n"
            f"  {len(catalog.USERS)} accounts, {len(catalog.NAMESPACES)} workspaces, "
            f"{sum(len(ns.collection.documents) for ns in catalog.NAMESPACES)} documents.\n"
            "Nothing is deleted or overwritten; anything already present is left alone.\n\n"
            f"Re-run with --yes, or set {CONFIRM_ENV_VAR}=1, to proceed.",
            file=sys.stderr,
        )
        return 1

    print(f"Seeding demo data into {target} (ENVIRONMENT={settings.environment})")

    try:
        if sys.platform == "win32":
            # psycopg's async driver cannot run on Windows' ProactorEventLoop,
            # which is the default. Same reason as run.py.
            with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                return runner.run(_run(args))
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Committed work is kept; nothing is rolled back.", file=sys.stderr)
        return 130


async def _run(args: argparse.Namespace) -> int:
    try:
        return await _seed(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
