"""Aggregates every route module into one router mounted at /api."""

from fastapi import APIRouter

from app.api.routes import chat, confluence, health, jobs, slack

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(confluence.router)
api_router.include_router(slack.router)
api_router.include_router(jobs.router)

__all__ = ["api_router"]
