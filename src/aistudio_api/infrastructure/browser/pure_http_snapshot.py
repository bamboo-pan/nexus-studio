"""Pure HTTP snapshot generation — no UI operations.

Uses chromium_botguard.py to generate BotGuard snapshots,
then builds the request body manually.
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("aistudio.snapshot")


def compute_content_hash(prompt: str) -> str:
    """Compute SHA-256 hash of prompt content."""
    return hashlib.sha256(prompt.encode()).hexdigest()


async def generate_snapshot_via_botguard(prompt: str) -> Optional[str]:
    """Generate BotGuard snapshot using chromium_botguard.py (no UI)."""
    from aistudio_api.infrastructure.browser.chromium_botguard import generate_snapshot
    
    content_hash = compute_content_hash(prompt)
    logger.info("Generating snapshot for hash: %s", content_hash[:16])
    
    try:
        result = await generate_snapshot(content_hash)
        snapshot = result.get("snapshot")
        if snapshot:
            logger.info("Snapshot generated: %d chars", len(snapshot))
            return snapshot
        else:
            logger.error("No snapshot in result: %s", result)
            return None
    except Exception as e:
        logger.error("Snapshot generation failed: %s", e)
        return None


def build_request_body(
    model: str,
    prompt: str,
    snapshot: str,
    system_instruction: Optional[str] = None,
    images: Optional[list] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> str:
    """Build AI Studio request body manually (no UI capture needed)."""
    
    # Build content parts
    parts = []
    if images:
        for img_b64, mime_type in images:
            parts.append({
                "inlineData": {
                    "mimeType": mime_type,
                    "data": img_b64,
                }
            })
    parts.append({"text": prompt})
    
    # Build contents
    contents = [{"role": "user", "parts": parts}]
    
    # Build system instruction
    system_instruction_content = None
    if system_instruction:
        system_instruction_content = {
            "role": "user",
            "parts": [{"text": system_instruction}],
        }
    
    # Build generation config
    generation_config = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if top_p is not None:
        generation_config["topP"] = top_p
    if top_k is not None:
        generation_config["topK"] = top_k
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    
    # Build tools
    tools_config = None
    if tools:
        tools_config = []
        for tool in tools:
            if isinstance(tool, dict):
                tools_config.append(tool)
    
    # Build the full request body (matching AI Studio's format)
    body = [
        model,  # model name
        contents,  # user contents
        None,  # unknown field
        None,  # generation config (will be filled below)
        snapshot,  # BotGuard snapshot
        None,  # unknown field
        None,  # unknown field
        None,  # unknown field
        None,  # unknown field
        None,  # unknown field
    ]
    
    # Fill generation config if provided
    if generation_config or tools_config:
        config = {}
        if generation_config:
            config.update(generation_config)
        if tools_config:
            config["tools"] = tools_config
        body[3] = config
    
    # Add system instruction if provided
    if system_instruction_content:
        body.insert(2, system_instruction_content)
    
    return json.dumps(body, ensure_ascii=False)


# Example usage
if __name__ == "__main__":
    import sys
    
    async def main():
        prompt = sys.argv[1] if len(sys.argv) > 1 else "hello world"
        
        print(f"Prompt: {prompt}")
        print(f"Content hash: {compute_content_hash(prompt)}")
        
        snapshot = await generate_snapshot_via_botguard(prompt)
        if snapshot:
            print(f"Snapshot: {snapshot[:80]}...")
            
            body = build_request_body(
                model="models/gemma-4-31b-it",
                prompt=prompt,
                snapshot=snapshot,
            )
            print(f"Body length: {len(body)}")
            print(f"Body preview: {body[:200]}...")
        else:
            print("Failed to generate snapshot")
    
    asyncio.run(main())
