"""Identity-checked cleanup of imported, unused OSTree images and base cache."""

from __future__ import annotations
from bazzite_portal.core.i18n import N_

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable

from bazzite_portal.maintenance import cancel_pending

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
REF_PATTERN = re.compile(r"^ostree/container/image/[A-Za-z0-9_./:@+-]{1,480}$")
MAX_REFS = 128
MAX_REF_OUTPUT = 512 * 1024
MAX_CHECKSUM_OUTPUT = 256


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "", "identity": "", "refs": []}


def _deployment_identity(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return None
    deployments = data["deployments"]
    if not deployments or len(deployments) > 64:
        return None
    if sum(isinstance(item, dict) and item.get("booted") is True for item in deployments) != 1:
        return None
    identity = []
    checksums = set()
    for item in deployments:
        if not isinstance(item, dict) or not isinstance(item.get("booted"), bool):
            return None
        deployment_id = item.get("id")
        checksum = item.get("checksum")
        osname = item.get("osname")
        if (not isinstance(deployment_id, str) or not deployment_id.strip()
                or len(deployment_id) > 256 or not isinstance(checksum, str)
                or not CHECKSUM_PATTERN.fullmatch(checksum)
                or not isinstance(osname, str) or not osname.strip() or len(osname) > 128):
            return None
        staged, pinned = item.get("staged"), item.get("pinned")
        if staged not in (None, True, False) or pinned not in (None, True, False):
            return None
        checksums.add(checksum.lower())
        identity.append({"id": deployment_id, "checksum": checksum.lower(), "osname": osname,
                         "booted": item["booted"], "staged": staged, "pinned": pinned})
    return identity


def _token(identity: list[dict[str, Any]], refs: list[dict[str, str]]) -> str:
    encoded = json.dumps({"deployments": identity, "refs": refs}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def target(data: Any, imported_refs: Any) -> dict[str, Any]:
    """Select only imported image refs whose commit is absent from deployments."""
    if not isinstance(data, dict) or data.get("transaction") not in (None, False, "", [], {}):
        return _unavailable(N_("An RPM-OSTree transaction is active or status is unavailable."))
    identity = _deployment_identity(data)
    if identity is None:
        return _unavailable("Deployment identities are missing, malformed, or ambiguous.")
    deployments = data["deployments"]
    if deployments[0]["booted"] is False:
        if deployments[0].get("staged") is True:
            return _unavailable(N_("Cancel the pending deployment before clearing the download cache."))
        return _unavailable(N_("Resolve the selected next-boot deployment before clearing the download cache."))
    if any(item["booted"] is False and item.get("staged") is True for item in deployments):
        return _unavailable(N_("Cancel the pending deployment before clearing the download cache."))
    if not isinstance(imported_refs, list) or len(imported_refs) > MAX_REFS:
        return _unavailable("Imported image references are unavailable or exceed the safe inspection limit.")
    retained = {item["checksum"] for item in identity}
    candidates = []
    seen = set()
    for entry in imported_refs:
        if (not isinstance(entry, dict) or not isinstance(entry.get("ref"), str)
                or not isinstance(entry.get("checksum"), str)):
            return _unavailable("An imported image reference could not be identified safely.")
        ref, checksum = entry["ref"], entry["checksum"]
        if not REF_PATTERN.fullmatch(ref) or not CHECKSUM_PATTERN.fullmatch(checksum) or ref in seen:
            return _unavailable("An imported image reference is malformed or duplicated.")
        seen.add(ref)
        if checksum.lower() not in retained:
            candidates.append({"ref": ref, "checksum": checksum.lower()})
    candidates.sort(key=lambda item: item["ref"])
    token = _token(identity, candidates)
    return {"available": True, "reason": "", "token": token,
            "identity": hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "refs": candidates}


def _read_status(run_command: Callable[..., str]) -> dict[str, Any]:
    try:
        data = json.loads(run_command(["rpm-ostree", "status", "--json"], timeout=15))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def _read_refs(data: dict[str, Any], run_command: Callable[..., str]) -> list[dict[str, str]]:
    text = run_command(["ostree", "refs"], timeout=30)
    if not isinstance(text, str) or len(text.encode("utf-8", errors="replace")) > MAX_REF_OUTPUT:
        raise RuntimeError("OSTree returned an oversized imported-reference list.")
    refs = [line.strip() for line in text.splitlines() if line.strip().startswith("ostree/container/image/")]
    if len(refs) > MAX_REFS:
        raise RuntimeError("Too many imported image references to inspect safely.")
    resolved = []
    for ref in refs:
        if not REF_PATTERN.fullmatch(ref):
            raise RuntimeError("OSTree returned a malformed imported image reference.")
        checksum = run_command(["ostree", "rev-parse", ref], timeout=5).strip()
        if len(checksum) > MAX_CHECKSUM_OUTPUT or not CHECKSUM_PATTERN.fullmatch(checksum):
            raise RuntimeError("OSTree could not resolve an imported image reference safely.")
        resolved.append({"ref": ref, "checksum": checksum})
    return resolved


def _current_target(run_command):
    data = _read_status(run_command)
    return data, target(data, _read_refs(data, run_command))


def execute(token: str, command_runner: Callable[..., str] = cancel_pending._command,
            lock_factory: Callable[[], Any] = cancel_pending._pending_lock) -> str:
    """Revalidate exact target refs, remove only those refs, then clean base cache."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid download-cache token.")
    with lock_factory():
        data, initial = _current_target(command_runner)
        if not initial["available"] or initial["token"] != token:
            raise RuntimeError(initial["reason"] or "The deployment or imported image references changed after confirmation.")
        active = cancel_pending._active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot clear the download cache while {active} is running.")
        current_data, current = _current_target(command_runner)
        if not current["available"] or current["token"] != token:
            raise RuntimeError(current["reason"] or "The deployment or imported image references changed before cleanup.")
        active = cancel_pending._active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot clear the download cache while {active} is running.")
        before = _deployment_identity(current_data)
        # Each argument is a ref from the bounded fixed namespace in the token.
        for entry in current["refs"]:
            command_runner(["ostree", "refs", "--delete", entry["ref"]], timeout=30)
        command_runner(["rpm-ostree", "cleanup", "--base"], timeout=None)
        after_data = _read_status(command_runner)
        after_identity = _deployment_identity(after_data)
        if after_identity != before:
            raise RuntimeError("Cache cleanup changed a booted, saved, or pinned deployment.")
        after_refs = _read_refs(after_data, command_runner)
        remaining = {entry["ref"] for entry in after_refs}
        if any(entry["ref"] in remaining for entry in current["refs"]):
            raise RuntimeError("One or more confirmed imported image references remain after cleanup.")
    return "Download cache cleanup completed. OSTree may retain shared objects used by other images."


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.cache_clear <64-character-token>", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1
    try:
        print(execute(argv[0]))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
