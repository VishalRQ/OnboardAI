"""FastAPI entrypoint.  Run: uvicorn app.main:app --reload"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.debug)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s starting up (backend=%s)", settings.app_name, settings.vector_backend)
    yield
    logger.info("%s shutting down", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description="RAG backend over internal onboarding knowledge (Confluence, Slack).",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before anything leaves localhost
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")


@app.get("/", tags=["health"])
async def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs"}
