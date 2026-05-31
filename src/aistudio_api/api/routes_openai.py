"""OpenAI-compatible API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from aistudio_api.application.api_service import handle_chat, handle_image_generation, handle_image_prompt_optimization, handle_messages, handle_messages_count_tokens, handle_openai_responses, parse_image_request
from aistudio_api.application.model_service import refresh_model_metadata
from aistudio_api.domain.model_capabilities import get_model_metadata, list_model_metadata
from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .dependencies import get_client
from .state import runtime_state
from .schemas import ChatRequest, ImagePromptOptimizationRequest

router = APIRouter()


@router.get("/v1/models")
async def list_models(refresh: bool = Query(False)):
    data = await refresh_model_metadata(runtime_state.client) if refresh else list_model_metadata()
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str):
    try:
        return get_model_metadata(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc), "type": "invalid_request_error"}) from exc


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request, client: AIStudioClient = Depends(get_client)):
    return await handle_chat(req, client, request=request)


@router.post("/v1/responses")
async def responses(req: dict, request: Request, client: AIStudioClient = Depends(get_client)):
    return await handle_openai_responses(req, client, request=request)


@router.post("/v1/messages")
async def messages(req: dict, request: Request, client: AIStudioClient = Depends(get_client)):
    return await handle_messages(req, client, request=request)


@router.post("/v1/messages/count_tokens")
async def messages_count_tokens(req: dict):
    return handle_messages_count_tokens(req)


@router.post("/v1/images/generations")
async def image_generations(req: Any = Body(...), client: AIStudioClient = Depends(get_client)):
    req = parse_image_request(req)
    return await handle_image_generation(req, client)


@router.post("/v1/images/prompt-optimizations")
async def image_prompt_optimizations(req: ImagePromptOptimizationRequest, client: AIStudioClient = Depends(get_client)):
    return await handle_image_prompt_optimization(req, client)

