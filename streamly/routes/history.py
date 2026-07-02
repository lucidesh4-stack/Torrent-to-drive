from fastapi import APIRouter
router = APIRouter(prefix="/api/history", tags=["history"])
@router.get("/")
async def history_root(): return {"status": "history route optimized"}
