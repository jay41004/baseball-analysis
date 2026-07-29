"""Cloud read-only mode: no background fetch, no keepalive storms (Render free tier)."""

from __future__ import annotations

import os


def is_cloud_lite() -> bool:
    return os.environ.get("CLOUD_LITE", "").lower() in {"1", "true", "yes"} or bool(
        os.environ.get("RENDER")
    )
