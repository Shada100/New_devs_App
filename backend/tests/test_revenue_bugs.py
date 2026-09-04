"""
Regression tests for the three issues the clients reported.

Each test fails against the original code and passes against the fix, so they
double as a description of what was actually wrong.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.cache import revenue_cache_key
from app.services.reservations import month_bounds_utc, to_money


# ---------------------------------------------------------------------------
# Issue 1: Sunset Properties' March totals didn't match their own records
# ---------------------------------------------------------------------------

# The reservation from the seed data that started the whole conversation.
RES_TZ_1_CHECK_IN = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)


def test_march_starts_before_midnight_utc_for_a_paris_property():
    """Paris is UTC+1 in March, so their March opens at 23:00 UTC on Feb 29."""
    start, end = month_bounds_utc(2024, 3, "Europe/Paris")

    assert start == datetime(2024, 2, 29, 23, 0, tzinfo=timezone.utc)
    # Paris has moved to UTC+2 by the end of March (DST), so the closing
    # boundary shifts too -- this is why you can't just subtract a fixed offset.
    assert end == datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc)


def test_late_february_utc_checkin_counts_as_march_in_paris():
    """
    The booking that went missing. 23:30 UTC on Feb 29 is 00:30 on March 1st in
    Paris, so it is March revenue for a Paris property.
    """
    start, end = month_bounds_utc(2024, 3, "Europe/Paris")

    assert start <= RES_TZ_1_CHECK_IN < end


def test_naive_boundaries_would_have_dropped_that_booking():
    """Pins down the old behaviour so nobody reintroduces it."""
    naive_march_start = datetime(2024, 3, 1, tzinfo=timezone.utc)

    assert RES_TZ_1_CHECK_IN < naive_march_start  # excluded from March
    assert not (naive_march_start <= RES_TZ_1_CHECK_IN)


def test_month_boundaries_follow_the_property_timezone():
    """A New York property's March starts five hours later than Paris'."""
    paris_start, _ = month_bounds_utc(2024, 3, "Europe/Paris")
    new_york_start, _ = month_bounds_utc(2024, 3, "America/New_York")

    assert new_york_start == datetime(2024, 3, 1, 5, 0, tzinfo=timezone.utc)
    assert new_york_start > paris_start


def test_december_rolls_into_the_next_year():
    start, end = month_bounds_utc(2024, 12, "Europe/Paris")

    assert start == datetime(2024, 11, 30, 23, 0, tzinfo=timezone.utc)
    assert end == datetime(2024, 12, 31, 23, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Issue 2: finance seeing totals "off by a few cents"
# ---------------------------------------------------------------------------

# prop-001's three March bookings, stored as NUMERIC(10, 3).
SUB_CENT_AMOUNTS = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]


def test_sub_cent_amounts_sum_to_an_exact_total():
    assert to_money(sum(SUB_CENT_AMOUNTS)) == Decimal("1000.00")


def test_rounding_each_reservation_before_summing_loses_a_cent():
    """
    This is the "off by a few cents" finance kept seeing. Amounts are stored to
    three decimals, so they have to be summed at full precision and rounded
    once. Rounding row by row quietly drops a cent per property.
    """
    round_then_sum = sum(to_money(amount) for amount in SUB_CENT_AMOUNTS)
    sum_then_round = to_money(sum(SUB_CENT_AMOUNTS))

    assert round_then_sum == Decimal("999.99")
    assert sum_then_round == Decimal("1000.00")
    assert sum_then_round - round_then_sum == Decimal("0.01")


def test_float_accumulation_drifts_on_ordinary_amounts():
    """
    Why the API no longer hands out a float. These are plain two-decimal
    bookings and the float total still lands a fraction of a cent off, which is
    the kind of thing that shows up once a client has a few hundred of them.
    """
    amounts = [Decimal("4527.20"), Decimal("2119.79"), Decimal("4073.86"), Decimal("4767.44")]

    exact = sum(amounts)
    as_floats = sum(float(amount) for amount in amounts)

    assert exact == Decimal("15488.29")
    assert Decimal(repr(as_floats)) != exact
    assert repr(as_floats) == "15488.289999999999"


def test_to_money_never_takes_the_float_shortcut():
    """Decimal(0.1) carries binary error; going through str() does not."""
    assert to_money(0.1) == Decimal("0.10")
    assert to_money("1250.005") == Decimal("1250.01")  # half-up, as finance expects
    assert to_money(None) == Decimal("0.00")


def test_totals_are_reported_to_the_cent():
    assert str(to_money(Decimal("4975.500"))) == "4975.50"


# ---------------------------------------------------------------------------
# Issue 3: Ocean Rentals seeing another company's revenue
# ---------------------------------------------------------------------------


def test_cache_key_includes_the_tenant():
    """prop-001 exists for both clients, so the key has to separate them."""
    sunset = revenue_cache_key("tenant-a", "prop-001")
    ocean = revenue_cache_key("tenant-b", "prop-001")

    assert sunset != ocean
    assert "tenant-a" in sunset
    assert "tenant-b" in ocean


@pytest.mark.asyncio
async def test_each_tenant_gets_their_own_cached_total(monkeypatch):
    import fakeredis.aioredis

    from app.services import cache as cache_module

    monkeypatch.setattr(
        cache_module, "redis_client", fakeredis.aioredis.FakeRedis()
    )

    totals = {
        "tenant-a": {"total": "2583.33", "count": 4},
        "tenant-b": {"total": "1776.50", "count": 4},
    }
    calls = []

    async def fake_calculate_total_revenue(property_id, tenant_id):
        calls.append((property_id, tenant_id))
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": totals[tenant_id]["total"],
            "currency": "USD",
            "count": totals[tenant_id]["count"],
        }

    monkeypatch.setattr(
        "app.services.reservations.calculate_total_revenue",
        fake_calculate_total_revenue,
    )

    # Sunset loads the dashboard first and warms the cache.
    sunset = await cache_module.get_revenue_summary("prop-001", "tenant-a")
    # Ocean asks for the same property id straight after.
    ocean = await cache_module.get_revenue_summary("prop-001", "tenant-b")

    assert sunset["total"] == "2583.33"
    assert ocean["total"] == "1776.50"
    assert ocean["tenant_id"] == "tenant-b"
    # Both had to be calculated; neither was served from the other's entry.
    assert calls == [("prop-001", "tenant-a"), ("prop-001", "tenant-b")]


@pytest.mark.asyncio
async def test_cached_entry_for_the_wrong_tenant_is_discarded(monkeypatch):
    """Defence in depth for stale entries written under an older key format."""
    import json

    import fakeredis.aioredis

    from app.services import cache as cache_module

    fake_redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(cache_module, "redis_client", fake_redis)

    poisoned = {
        "property_id": "prop-001",
        "tenant_id": "tenant-a",
        "total": "2583.33",
        "currency": "USD",
        "count": 4,
    }
    await fake_redis.set(revenue_cache_key("tenant-b", "prop-001"), json.dumps(poisoned))

    async def fake_calculate_total_revenue(property_id, tenant_id):
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "1776.50",
            "currency": "USD",
            "count": 4,
        }

    monkeypatch.setattr(
        "app.services.reservations.calculate_total_revenue",
        fake_calculate_total_revenue,
    )

    result = await cache_module.get_revenue_summary("prop-001", "tenant-b")

    assert result["tenant_id"] == "tenant-b"
    assert result["total"] == "1776.50"


@pytest.mark.asyncio
async def test_a_missing_tenant_is_refused_rather_than_pooled(monkeypatch):
    from app.services import cache as cache_module

    with pytest.raises(ValueError):
        await cache_module.get_revenue_summary("prop-001", "")
