"""Wipe the shared job pool and everything derived from it.

Why this exists as a script rather than a paste-able SQL snippet: the delete order
matters and getting it wrong fails halfway, leaving the database in a worse state
than before. `fit_scores` and `applications` hold foreign keys to
`job_postings.external_id`, so deleting postings first raises

    ForeignKeyViolation: update or delete on table "job_postings" violates foreign
    key constraint ... referenced from table "fit_scores"

and the tables without a formal foreign key still hold `external_id` values that
would be orphaned. This deletes all of them, in the order that works.

What survives: accounts, sessions, uploaded documents, profiles, gap reports,
plans and payments. Only the job pool and per-job derived data go.

Usage, from the deploy directory on the server:

    docker compose exec app python deploy/reset-jobs.py --yes

Without --yes it only reports what it would delete.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from saas import schema as s  # noqa: E402
from saas.engine import get_engine  # noqa: E402

# Order is the point of this file. Children first, postings last.
ORDER = [
    ("fit_scores", s.fit_scores),        # FK -> job_postings.external_id
    ("applications", s.applications),    # FK -> job_postings.external_id
    ("artifacts", s.artifacts),          # external_id, no FK, would orphan
    ("comp_estimates", s.comp_estimates),
    ("mock_sessions", s.mock_sessions),
    ("outcomes", s.outcomes),
    ("job_postings", s.job_postings),    # last
]


def main() -> int:
    confirm = "--yes" in sys.argv
    engine = get_engine()

    with engine.begin() as c:
        counts = {
            name: int(c.execute(select(func.count()).select_from(table)).scalar_one())
            for name, table in ORDER
        }

    print("current rows")
    for name, _ in ORDER:
        print(f"  {name:16} {counts[name]:>8,}")

    if not confirm:
        print("\nDry run. Nothing deleted. Add --yes to actually wipe.")
        print("Accounts, documents, profiles, plans and payments are never touched.")
        return 0

    print("\ndeleting")
    with engine.begin() as c:
        for name, table in ORDER:
            result = c.execute(delete(table))
            print(f"  {name:16} {result.rowcount if result.rowcount != -1 else counts[name]:>8,} removed")

    with engine.begin() as c:
        left = int(c.execute(select(func.count()).select_from(s.job_postings)).scalar_one())
        users = int(c.execute(select(func.count()).select_from(s.users)).scalar_one())
        docs = int(c.execute(select(func.count()).select_from(s.documents)).scalar_one())

    print(f"\ndone: {left} postings left, {users} accounts and {docs} documents intact")
    print('Next: sign in and press "Search for new openings".')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
