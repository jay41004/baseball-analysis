"""Keep Render web service warm and caches populated."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = int(os.environ.get("KEEPALIVE_SECONDS", "600"))


async def cloud_keepalive_loop() -> None:
    if not os.environ.get("RENDER"):
        return

    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        logger.warning("RENDER is set but RENDER_EXTERNAL_URL is missing; skip keepalive")
        return

    await asyncio.sleep(30)
    logger.info("Cloud keepalive started for %s", base_url)

    while True:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                health = await client.get(f"{base_url}/health")
                warmup = await client.get(f"{base_url}/api/warmup")
                logger.info(
                    "Keepalive ping health=%s warmup=%s",
                    health.status_code,
                    warmup.status_code,
                )
        except Exception:
            logger.exception("Keepalive ping failed")

        await asyncio.sleep(KEEPALIVE_SECONDS)
