"""
End-to-end checks against a real Postgres and Redis holding the seeded data.

Skipped unless REVENUE_INTEGRATION_DB is set, so the normal unit run stays
dependency free:

    docker compose up -d db redis
    REVENUE_INTEGRATION_DB=1 \
    DATABASE_URL=postgresql://postgres:postgres@localhost:5433/propertyflow \
    REDIS_URL=redis://localhost:6380/0 \
    python -m pytest tests/test_integration_revenue.py -v
"""

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    not os.getenv("REVENUE_INTEGRATION_DB"),
    reason="needs the seeded database; set REVENUE_INTEGRATION_DB=1",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_cache():
    """Each test starts with an empty cache so ordering can't hide a leak."""
    from app.services import cache as cache_module

    await cache_module.redis_client.flushdb()
    yield
    await cache_module.redis_client.flushdb()


# ---------------------------------------------------------------------------
# Sunset Properties' March
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_march_for_a_paris_property_includes_the_late_february_checkin():
    """
    prop-001 has four March bookings once you count in Paris time: the
    2024-02-29 23:30 UTC arrival (1250.000) plus three mid-March stays
    (333.333 + 333.333 + 333.334 = 1000.000).
    """
    from app.services.reservations import calculate_monthly_revenue

    total = await calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024)

    assert total == Decimal("2250.00")


@pytest.mark.asyncio
async def test_utc_boundaries_would_have_shown_1250_less():
    """
    Reproduces the old behaviour directly against the database so the size of
    the discrepancy is visible: this is the number the client was disputing.
    """
    from sqlalchemy import text

    from app.core.database_pool import db_pool
    from app.services.reservations import to_money

    async with db_pool.get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT SUM(total_amount) AS total
                FROM reservations
                WHERE property_id = :p AND tenant_id = :t
                  AND check_in_date >= :start AND check_in_date < :end
                """
            ),
            {
                "p": "prop-001",
                "t": "tenant-a",
                # What datetime(2024, 3, 1) becomes once Postgres compares it.
                "start": datetime(2024, 3, 1, tzinfo=timezone.utc),
                "end": datetime(2024, 4, 1, tzinfo=timezone.utc),
            },
        )
        naive_total = to_money(result.scalar())

    assert naive_total == Decimal("1000.00")
    assert Decimal("2250.00") - naive_total == Decimal("1250.00")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prop_001_returns_different_data_per_tenant():
    """Same property id, two different properties, two different answers."""
    from app.services.reservations import calculate_total_revenue

    sunset = await calculate_total_revenue("prop-001", "tenant-a")
    ocean = await calculate_total_revenue("prop-001", "tenant-b")

    assert sunset["total"] == "2250.00"
    assert sunset["count"] == 4
    # Ocean's Mountain Lodge Beta has no reservations seeded.
    assert ocean["total"] == "0.00"
    assert ocean["count"] == 0


@pytest.mark.asyncio
async def test_a_warm_cache_does_not_leak_across_tenants():
    """
    The exact sequence Ocean Rentals reported: Sunset loads the dashboard, then
    Ocean refreshes and asks for the same property id.
    """
    from app.services.cache import get_revenue_summary

    sunset = await get_revenue_summary("prop-001", "tenant-a")
    ocean = await get_revenue_summary("prop-001", "tenant-b")

    assert sunset["total"] == "2250.00"
    assert ocean["total"] == "0.00"  # was 2250.00 before the fix
    assert ocean["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_a_tenant_cannot_read_a_property_it_does_not_own():
    from app.services.reservations import PropertyNotFound, calculate_total_revenue

    # prop-002 belongs to Sunset only.
    with pytest.raises(PropertyNotFound):
        await calculate_total_revenue("prop-002", "tenant-b")


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "property_id, tenant_id, expected_total, expected_count",
    [
        ("prop-002", "tenant-a", "4975.50", 4),
        ("prop-003", "tenant-a", "6100.50", 2),
        ("prop-004", "tenant-b", "1776.50", 4),
        ("prop-005", "tenant-b", "3256.00", 3),
    ],
)
async def test_totals_are_exact_to_the_cent(
    property_id, tenant_id, expected_total, expected_count
):
    from app.services.reservations import calculate_total_revenue

    result = await calculate_total_revenue(property_id, tenant_id)

    assert result["total"] == expected_total
    assert result["count"] == expected_count


@pytest.mark.asyncio
async def test_sub_cent_amounts_add_up_without_losing_anything():
    """
    prop-001's three mid-March stays are stored to three decimals. Summed at
    full precision they are exactly 1000.00; rounded row by row they would be
    999.99.
    """
    from app.services.reservations import calculate_monthly_revenue

    # March in New York time excludes the Feb 29 arrival, isolating the three
    # sub-cent bookings -- handy for showing the rounding on its own.
    total = await calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024)

    assert total == Decimal("2250.00")
    assert total - Decimal("1250.00") == Decimal("1000.00")
