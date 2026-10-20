"""Shared rpm-ostree deployment status reader."""

from __future__ import annotations

import json
from typing import Any, Callable


def read_status(run_command: Callable[..., str], *, max_bytes: int | None = None) -> dict[str, Any]:
    """Read deployment status, optionally rejecting output beyond a byte limit."""
    try:
        raw = run_command(["rpm-ostree", "status", "--json"], timeout=15)
        if max_bytes is not None and (
            not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > max_bytes
        ):
            raise ValueError("oversized status")
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data
