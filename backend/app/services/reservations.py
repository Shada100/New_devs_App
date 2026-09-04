from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database_pool import db_pool

UTC = ZoneInfo("UTC")

# Amounts are stored as NUMERIC(10, 3) so we can keep sub-cent precision while
# summing, but anything we report has to land on a real cent.
CENT = Decimal("0.01")


class PropertyNotFound(Exception):
    """Raised when a property does not exist for the requesting tenant."""


class MixedCurrencyError(Exception):
    """Raised when a property has reservations booked in more than one currency."""


def to_money(value: Any) -> Decimal:
    """
    Convert a raw DB amount to a Decimal rounded to the cent.

    Going via str() matters: Decimal(float) drags the binary rounding error along
    with it, which is where the "off by a few cents" reports came from.
    """
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def month_bounds_utc(year: int, month: int, timezone_name: str) -> Tuple[datetime, datetime]:
    """
    Return the UTC half-open range [start, end) covering the given month as the
    property's own clock sees it.

    A booking checking in at 2024-02-29 23:30 UTC is already 2024-03-01 00:30 in
    Paris, so for a Paris property it belongs to March. Building the boundaries
    from naive datetimes treats them as UTC and drops that booking from the month.
    """
    tz = ZoneInfo(timezone_name)

    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)

    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def get_property_timezone(session: AsyncSession, property_id: str, tenant_id: str) -> str:
    """
    Look up a property's timezone, scoped to the tenant.

    Property IDs are only unique per tenant (prop-001 exists for both clients),
    so the tenant_id is part of the lookup, not an afterthought.
    """
    query = text(
        """
        SELECT timezone
        FROM properties
        WHERE id = :property_id AND tenant_id = :tenant_id
        """
    )
    result = await session.execute(query, {"property_id": property_id, "tenant_id": tenant_id})
    row = result.fetchone()

    if row is None:
        raise PropertyNotFound(f"Property {property_id} not found for tenant {tenant_id}")

    return row.timezone or "UTC"


async def calculate_monthly_revenue(
    property_id: str,
    tenant_id: str,
    month: int,
    year: int,
    session: Optional[AsyncSession] = None,
) -> Decimal:
    """
    Calculate revenue for a specific month, using the property's local calendar.
    """
    if session is None:
        async with db_pool.get_session() as owned_session:
            return await calculate_monthly_revenue(property_id, tenant_id, month, year, owned_session)

    timezone_name = await get_property_timezone(session, property_id, tenant_id)
    start_utc, end_utc = month_bounds_utc(year, month, timezone_name)

    query = text(
        """
        SELECT SUM(total_amount) AS total
        FROM reservations
        WHERE property_id = :property_id
          AND tenant_id = :tenant_id
          AND check_in_date >= :start_date
          AND check_in_date < :end_date
        """
    )
    result = await session.execute(
        query,
        {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "start_date": start_utc,
            "end_date": end_utc,
        },
    )

    return to_money(result.scalar())


async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregate all-time revenue for a property belonging to a tenant.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required to calculate revenue")

    async with db_pool.get_session() as session:
        # Confirms the property belongs to this tenant before we report on it.
        await get_property_timezone(session, property_id, tenant_id)

        query = text(
            """
            SELECT
                currency,
                SUM(total_amount) AS total_revenue,
                COUNT(*) AS reservation_count
            FROM reservations
            WHERE property_id = :property_id AND tenant_id = :tenant_id
            GROUP BY currency
            """
        )
        result = await session.execute(
            query, {"property_id": property_id, "tenant_id": tenant_id}
        )
        rows = result.fetchall()

    if not rows:
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "0.00",
            "currency": "USD",
            "count": 0,
        }

    if len(rows) > 1:
        # Adding EUR to USD would produce a number that means nothing. Better to
        # fail loudly than to hand finance a total they can't reconcile.
        currencies = ", ".join(sorted(row.currency for row in rows))
        raise MixedCurrencyError(
            f"Property {property_id} has reservations in multiple currencies ({currencies})"
        )

    row = rows[0]
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": str(to_money(row.total_revenue)),
        "currency": row.currency or "USD",
        "count": row.reservation_count,
    }
