import json
import os
from typing import Any, Dict

import redis.asyncio as redis

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

CACHE_TTL_SECONDS = 300


def revenue_cache_key(tenant_id: str, property_id: str) -> str:
    """
    Build the cache key for a property's revenue summary.

    The tenant has to be in the key. Property IDs are only unique within a tenant
    -- prop-001 is Sunset's "Beach House Alpha" and Ocean's "Mountain Lodge Beta"
    -- so a key of just revenue:prop-001 means whichever client loads the
    dashboard first fills the cache and the other one reads their numbers.
    """
    return f"revenue:{tenant_id}:{property_id}"


async def get_revenue_summary(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required to read a revenue summary")

    cache_key = revenue_cache_key(tenant_id, property_id)

    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # Belt and braces: if an entry ever lands under the wrong key (an old
        # key format left in Redis, say), drop it rather than serve it.
        if payload.get("tenant_id") == tenant_id:
            return payload
        await redis_client.delete(cache_key)

    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_total_revenue

    # Calculate revenue
    result = await calculate_total_revenue(property_id, tenant_id)

    # Cache the result for 5 minutes
    await redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)

    return result


async def invalidate_revenue_summary(property_id: str, tenant_id: str) -> None:
    """Drop a cached summary, e.g. after reservations for the property change."""
    await redis_client.delete(revenue_cache_key(tenant_id, property_id))
