from fastapi import APIRouter

router = APIRouter()


@router.get("/dashboard")
async def dashboard():
    """`crm.quotes.stats` plus booked shipments and the latest `score` events."""
    raise NotImplementedError
