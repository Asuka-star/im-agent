from fastapi import APIRouter

from app.api.routes.feishu import router as feishu_router
from app.api.routes.health import router as health_router
from app.api.routes.workflow import router as workflow_router


api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(feishu_router, prefix="/feishu", tags=["feishu"])
api_router.include_router(workflow_router, prefix="/workflow", tags=["workflow"])
