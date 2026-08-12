"""Direct-SQL org membership path (standalone database, no platform reader).

The Hub's active plan can differ from the profile's default org, so memberships
win over `user_profiles`. Passing `platform_reader=None` pins these tests to the
SQL fallback; `test_platform_memberships.py` covers the service role path.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest

from smart_qr.adapters.db import repositories
from smart_qr.adapters.db.repositories import OrgMembershipRepository

USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DEFAULT_ORG = UUID("11111111-1111-1111-1111-111111111111")
SWITCHED_ORG = UUID("22222222-2222-2222-2222-222222222222")
FOREIGN_ORG = UUID("33333333-3333-3333-3333-333333333333")


class FakePool:
    """Answers the two queries the repository makes, or raises for a missing table."""

    def __init__(self, *, memberships, profile_org, memberships_table=True, profile_table=True):
        self.memberships = memberships
        self.profile_org = profile_org
        self.memberships_table = memberships_table
        self.profile_table = profile_table
        self.queries: list[str] = []

    async def fetchrow(self, sql, *args):
        if "user_organization_memberships" in sql:
            self.queries.append("memberships")
            if not self.memberships_table:
                raise asyncpg.UndefinedTableError("no such table")
            return {"?column?": 1} if (args[0], args[1]) in self.memberships else None

        self.queries.append("profile")
        if not self.profile_table:
            raise asyncpg.UndefinedTableError("no such table")
        if self.profile_org is None:
            return None
        return {"organization_id": str(self.profile_org)}


@pytest.fixture
def use_pool(monkeypatch):
    def _install(pool):
        monkeypatch.setattr(repositories, "get_pool", lambda: pool)
        return pool

    return _install


@pytest.mark.asyncio
async def test_membership_row_is_enough(use_pool):
    """A user on a switched active plan is a member, but not via user_profiles."""
    pool = use_pool(
        FakePool(memberships={(USER, str(SWITCHED_ORG))}, profile_org=DEFAULT_ORG)
    )
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, SWITCHED_ORG) is True
    assert pool.queries == ["memberships"]


@pytest.mark.asyncio
async def test_profile_org_is_accepted_without_a_membership_row(use_pool):
    use_pool(FakePool(memberships=set(), profile_org=DEFAULT_ORG))
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, DEFAULT_ORG) is True


@pytest.mark.asyncio
async def test_foreign_org_is_refused(use_pool):
    use_pool(FakePool(memberships=set(), profile_org=DEFAULT_ORG))
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, FOREIGN_ORG) is False


@pytest.mark.asyncio
async def test_missing_profile_row_is_refused(use_pool):
    use_pool(FakePool(memberships=set(), profile_org=None))
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, DEFAULT_ORG) is False


@pytest.mark.asyncio
async def test_falls_back_to_profile_when_memberships_table_is_absent(use_pool):
    use_pool(
        FakePool(memberships=set(), profile_org=DEFAULT_ORG, memberships_table=False)
    )
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, DEFAULT_ORG) is True


@pytest.mark.asyncio
async def test_unknown_when_neither_table_exists(use_pool):
    """Standalone Postgres without the platform tables: unknown, not a denial."""
    use_pool(
        FakePool(
            memberships=set(),
            profile_org=None,
            memberships_table=False,
            profile_table=False,
        )
    )
    assert await OrgMembershipRepository(platform_reader=None).user_belongs_to_org(USER, DEFAULT_ORG) is None
