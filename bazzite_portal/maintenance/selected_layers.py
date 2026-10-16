"""Fixed root helper for removing a confirmed subset of requested RPM layers."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from bazzite_portal.maintenance import reset_layers

INSTANCE_PATTERN = re.compile(r"^([0-9a-f]{64})-([0-9a-f]{64})$")


def decode_selection(data: Any, token: str, bitmap: str) -> list[str]:
    """Resolve a bounded bitset against the exact sorted request list."""
    if not isinstance(token, str) or not reset_layers.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid package-layer removal token.")
    if not isinstance(bitmap, str) or not re.fullmatch(r"[0-9a-f]{64}", bitmap):
        raise ValueError("Invalid package selection bitmap.")
    target = reset_layers.target(data)
    if not target["available"] or target["token"] != token:
        raise RuntimeError(target["reason"] or "The requested package list changed after confirmation.")
    bits = int(bitmap, 16)
    if bits == 0 or bits >> len(target["packages"]):
        raise ValueError("Package selection is empty or outside the confirmed request list.")
    return [name for index, name in enumerate(target["packages"]) if bits & (1 << index)]


def execute(token: str, bitmap: str) -> str:
    if not isinstance(token, str) or not reset_layers.TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid package-layer removal token.")
    if not isinstance(bitmap, str) or not re.fullmatch(r"[0-9a-f]{64}", bitmap):
        raise ValueError("Invalid package selection bitmap.")
    data = reset_layers._read_status(reset_layers._command)
    chosen = decode_selection(data, token, bitmap)
    return reset_layers.execute(token, selected_packages=chosen)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not INSTANCE_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.selected_layers <token>-<bitmap>", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1
    token, bitmap = argv[0].split("-", 1)
    try:
        print(execute(token, bitmap))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
