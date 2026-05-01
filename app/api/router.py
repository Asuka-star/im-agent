from fastapi import APIRouter

from app.api.routes.artifacts import router as artifacts_router
from app.api.routes.feishu import router as feishu_router
from app.api.routes.health import router as health_router
from app.api.routes.realtime import router as realtime_router
from app.api.routes.task_runs import router as task_runs_router
from app.api.routes.workflow import router as workflow_router


api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(artifacts_router, prefix="/artifacts", tags=["artifacts"])
api_router.include_router(feishu_router, prefix="/feishu", tags=["feishu"])
api_router.include_router(realtime_router, prefix="/ws", tags=["realtime"])
api_router.include_router(task_runs_router, prefix="/task-runs", tags=["task-runs"])
api_router.include_router(workflow_router, prefix="/workflow", tags=["workflow"])
