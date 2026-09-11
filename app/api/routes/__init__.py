"""Aggregates every route module into one router mounted at /api."""

from fastapi import APIRouter

from app.api.routes import chat, confluence, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(confluence.router)

__all__ = ["api_router"]
