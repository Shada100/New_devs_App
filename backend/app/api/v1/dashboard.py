from fastapi import APIRouter, Depends, HTTPException
from typing import Any, Dict

from app.services.cache import get_revenue_summary
from app.services.reservations import MixedCurrencyError, PropertyNotFound
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:

    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        # Falling back to a shared "default_tenant" put every account without a
        # resolved tenant into the same bucket, mixing clients' revenue. If we
        # don't know who is asking, we don't answer.
        raise HTTPException(status_code=403, detail="No tenant associated with this account")

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id)
    except PropertyNotFound:
        raise HTTPException(status_code=404, detail="Property not found")
    except MixedCurrencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {
        "property_id": revenue_data["property_id"],
        # Money stays a decimal string. Casting to float here was rounding
        # totals at the last step and is what finance saw as missing cents.
        "total_revenue": revenue_data["total"],
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"],
    }
