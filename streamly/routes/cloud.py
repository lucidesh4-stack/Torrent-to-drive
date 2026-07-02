from fastapi import APIRouter
router = APIRouter(prefix="/api/cloud", tags=["cloud"])
@router.get("/")
async def cloud_root(): return {"status": "cloud route optimized"}
