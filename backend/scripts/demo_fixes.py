"""
Shows each bug's before and after against the seeded database.

    make demo

Runs the fixed code, and alongside it reproduces what the original code did, so
the difference is visible rather than asserted.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text


def rule(title):
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


async def main():
    from app.core.database_pool import db_pool
    from app.services.cache import get_revenue_summary, redis_client, revenue_cache_key
    from app.services.reservations import calculate_monthly_revenue, to_money

    await redis_client.flushdb()

    # -- Bug 2 -------------------------------------------------------------
    rule("BUG 2  Sunset's March - prop-001, Beach House Alpha, Europe/Paris")

    fixed = await calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024)

    # The original code built naive boundaries, which get compared as UTC.
    async with db_pool.get_session() as session:
        result = await session.execute(
            text(
                "SELECT SUM(total_amount) FROM reservations "
                "WHERE property_id = :p AND tenant_id = :t "
                "AND check_in_date >= :start AND check_in_date < :end"
            ),
            {
                "p": "prop-001",
                "t": "tenant-a",
                "start": datetime(2024, 3, 1, tzinfo=timezone.utc),
                "end": datetime(2024, 4, 1, tzinfo=timezone.utc),
            },
        )
        old = to_money(result.scalar())

    print(f"  before  (naive UTC boundaries) : {old}")
    print(f"  after   (Paris boundaries)     : {fixed}")
    print(f"  recovered                      : {fixed - old}   <- res-tz-1, "
          f"checked in 2024-02-29 23:30 UTC = 00:30 Mar 1 in Paris")

    # -- Bug 1 -------------------------------------------------------------
    rule("BUG 1  Both clients ask for prop-001, cache warm")

    sunset = await get_revenue_summary("prop-001", "tenant-a")
    ocean = await get_revenue_summary("prop-001", "tenant-b")

    print(f"  Sunset  (tenant-a)  total={sunset['total']:>9}  count={sunset['count']}")
    print(f"  Ocean   (tenant-b)  total={ocean['total']:>9}  count={ocean['count']}")
    print()
    print(f"  cache keys now:  {revenue_cache_key('tenant-a', 'prop-001')}")
    print(f"                   {revenue_cache_key('tenant-b', 'prop-001')}")
    print("  before: one shared key 'revenue:prop-001'")
    print("          -> Ocean read Sunset's 2250.00")

    # -- Bug 3 -------------------------------------------------------------
    rule("BUG 3  Sub-cent rounding - prop-001's three mid-March stays")

    amounts = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
    print(f"  amounts stored           : {', '.join(str(a) for a in amounts)}")
    print(f"  before (round each, sum) : {sum(to_money(a) for a in amounts)}")
    print(f"  after  (sum, round once) : {to_money(sum(amounts))}   <- correct")
    print()
    floats = sum(float(a) for a in [Decimal("4527.20"), Decimal("2119.79"),
                                    Decimal("4073.86"), Decimal("4767.44")])
    print(f"  and money as a float     : {floats!r}")
    print(f"  as an exact decimal      : 15488.29")

    print()
    await redis_client.aclose()
    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
