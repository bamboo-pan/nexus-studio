"""Gemini-compatible API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from aistudio_api.application.api_service import gemini_count_tokens_response, gemini_model_dict, handle_gemini_generate_content, list_gemini_models_response
from aistudio_api.application.model_service import refresh_model_metadata
from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .dependencies import get_client
from .schemas import GeminiGenerateContentRequest
from .state import runtime_state

router = APIRouter()


def _unsupported(message: str) -> HTTPException:
    return HTTPException(status_code=501, detail={"message": message, "type": "unsupported_feature"})


@router.get("/v1beta/models")
async def list_models(refresh: bool = Query(False)):
    if refresh:
        await refresh_model_metadata(runtime_state.client)
    return list_gemini_models_response()


@router.get("/v1beta/models/{model_id:path}")
async def get_model(model_id: str):
    try:
        return gemini_model_dict(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc), "type": "not_found"}) from exc


@router.post("/v1beta/{model_path:path}:countTokens")
async def count_tokens(model_path: str, req: dict):
    try:
        return gemini_count_tokens_response(model_path, req)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc), "type": "bad_request"}) from exc


@router.post("/v1beta/{model_path:path}:embedContent")
async def embed_content(model_path: str, req: dict):
    raise _unsupported("Gemini embeddings are not supported by AI Studio browser replay mode yet")


@router.post("/v1beta/{model_path:path}:batchEmbedContents")
async def batch_embed_contents(model_path: str, req: dict):
    raise _unsupported("Gemini batch embeddings are not supported by AI Studio browser replay mode yet")


@router.post("/v1beta/{model_path:path}:generateContent")
async def generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient = Depends(get_client),
):
    return await handle_gemini_generate_content(model_path, req, client, stream=False)


@router.post("/v1beta/{model_path:path}:streamGenerateContent")
async def stream_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    request: Request,
    client: AIStudioClient = Depends(get_client),
):
    return await handle_gemini_generate_content(model_path, req, client, stream=True, request=request)
