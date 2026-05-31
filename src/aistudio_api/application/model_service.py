"""Model list refresh helpers."""

from __future__ import annotations

import logging

from aistudio_api.domain.model_capabilities import list_model_metadata, register_dynamic_models

logger = logging.getLogger("aistudio.models")


async def refresh_model_metadata(client) -> list[dict]:
    list_models = getattr(client, "list_available_models", None)
    if callable(list_models):
        try:
            register_dynamic_models(await list_models())
        except Exception as exc:
            logger.warning("Dynamic model refresh failed; using cached registry: %s", exc)
    return list_model_metadata()