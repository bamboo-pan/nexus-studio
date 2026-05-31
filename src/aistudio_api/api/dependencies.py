"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import HTTPException

from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .state import runtime_state


def get_client() -> AIStudioClient:
    if runtime_state.client is None:
        raise HTTPException(503, detail={"message": "Client not initialized", "type": "service_unavailable"})
    return runtime_state.client


def get_busy_lock():
    if runtime_state.busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    return runtime_state.busy_lock


def get_account_service():
    if runtime_state.account_service is None:
        raise HTTPException(503, detail={"message": "Account service not initialized", "type": "service_unavailable"})
    return runtime_state.account_service


def get_runtime_state():
    return runtime_state

