"""Optional cloud read-only mode (disable warm-all / keepalive storms).

Set CLOUD_LITE=1 to enable. CLOUD_LITE=0 (or unset) keeps on-demand refresh working.
RENDER alone no longer forces read-only — that previously blocked all updates on free tier.
"""

from __future__ import annotations

import os


def is_cloud_lite() -> bool:
    flag = os.environ.get("CLOUD_LITE", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return False
