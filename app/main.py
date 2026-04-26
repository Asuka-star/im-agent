import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.database import init_db
from app.services.realtime_hub import realtime_hub


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_db()
    realtime_hub.bind_loop(asyncio.get_running_loop())
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api")


@app.get("/", tags=["root"])
async def root() -> dict[str, str]:
    return {"message": f"{settings.app_name} is running"}
