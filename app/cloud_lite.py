"""Cloud lite mode: keep Render free tier from OOM crash loops.

Priority:
1. CLOUD_LITE=0/false → full mode
2. CLOUD_LITE=1/true → lite mode
3. Else if RENDER is set → lite mode (default for free tier)
"""

from __future__ import annotations

import os


def is_cloud_lite() -> bool:
    flag = os.environ.get("CLOUD_LITE", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return bool(os.environ.get("RENDER"))
