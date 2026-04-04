from fastapi import APIRouter

router = APIRouter(tags=["System"])


@router.get("/health", summary="Health check")
async def health():
    return {"status": "ok", "service": "Rausch PT Tebra API"}
