"""Identity-checked restoration of the running OSTree deployment as boot default."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Callable

from bazzite_portal.maintenance import cancel_pending

MAX_DEPLOYMENTS = 64
MAX_IDENTITY_BYTES = 128 * 1024
TOKEN_PATTERN = cancel_pending.TOKEN_PATTERN


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "token": "", "reason": reason[:1024],
            "version": "Unknown", "identity": ""}


def _text(value: Any, limit: int, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= limit
            and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _snapshot(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or "deployments" not in data:
        raise ValueError("RPM-OSTree deployment data is unavailable.")
    if cancel_pending._has_transaction(data.get("transaction")):
        raise ValueError("An RPM-OSTree transaction is active.")
    entries = data["deployments"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_DEPLOYMENTS:
        raise ValueError("RPM-OSTree deployment list is missing, empty, or too large.")
    identities = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("RPM-OSTree deployment list is malformed.")
        deployment_id, checksum = entry.get("id"), entry.get("checksum")
        osname, reference = entry.get("osname"), entry.get("container-image-reference", "")
        if (not _text(deployment_id, 256) or deployment_id != deployment_id.strip()
                or not isinstance(checksum, str) or not cancel_pending.CHECKSUM_PATTERN.fullmatch(checksum)
                or not _text(osname, 256) or osname != osname.strip()
                or not _text(reference, 2048, empty=True)
                or not isinstance(entry.get("booted"), bool)
                or not isinstance(entry.get("pinned"), bool)
                or not isinstance(entry.get("staged", False), bool)):
            raise ValueError("A deployment identity or state is incomplete or malformed.")
        if deployment_id in seen:
            raise ValueError("Deployment identities are ambiguous.")
        seen.add(deployment_id)
        if entry.get("staged", False):
            raise ValueError("A staged deployment must be resolved before canceling rollback.")
        identities.append({"id": deployment_id, "checksum": checksum.lower(),
                           "osname": osname, "container-image-reference": reference,
                           "booted": entry["booted"], "pinned": entry["pinned"], "staged": False})
    if sum(entry["booted"] for entry in identities) != 1:
        raise ValueError("Expected exactly one running deployment.")
    if len(_encoded(identities)) > MAX_IDENTITY_BYTES:
        raise ValueError("Deployment identity data is too large to confirm safely.")
    return identities


def _encoded(identities: list[dict[str, Any]]) -> bytes:
    return json.dumps(identities, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def target(data: Any) -> dict[str, Any]:
    """Describe a non-staged next-boot selection different from the running one."""
    try:
        identities = _snapshot(data)
        booted_index = next(index for index, item in enumerate(identities) if item["booted"])
        if booted_index == 0:
            return _unavailable("No rollback is scheduled.")
        if identities[0]["osname"] != identities[booted_index]["osname"]:
            return _unavailable("The selected and running deployments belong to different stateroots.")
    except ValueError as exc:
        return _unavailable(str(exc))
    return {"available": True, "token": hashlib.sha256(_encoded(identities)).hexdigest(),
            "reason": "", "version": cancel_pending._clean(data["deployments"][booted_index].get("version")),
            "identity": identities[booted_index]["checksum"]}


def execute(token: str, command_runner: Callable[..., str] = cancel_pending._command,
            lock_factory: Callable[[], Any] = cancel_pending._pending_lock) -> str:
    """Restore the confirmed running deployment without deleting any saved one."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid rollback cancellation token.")
    # Every privileged maintenance helper shares this flock. The fixed busy-unit
    # list checks UUPD and rollback, not this helper's own cancel-rollback unit.
    with lock_factory():
        current = None
        for _ in range(2):
            current = cancel_pending._read_status(command_runner)
            candidate = target(current)
            if not candidate["available"] or candidate["token"] != token:
                raise RuntimeError(candidate["reason"] or "The deployment list changed after confirmation.")
            active = cancel_pending._active_operation(command_runner)
            if active:
                raise RuntimeError(f"Cannot cancel rollback while {active} is running.")
        assert current is not None
        before = _snapshot(current)
        index = next(index for index, item in enumerate(before) if item["booted"])
        if command_runner is cancel_pending._command:
            print("Restoring the running deployment as the next boot default...", flush=True)
        command_runner(["ostree", "admin", "set-default", str(index)], timeout=None)
        after_data = cancel_pending._read_status(command_runner)
        try:
            after = _snapshot(after_data)
        except ValueError as exc:
            raise RuntimeError(f"Could not verify rollback cancellation: {exc}") from exc
        expected = [before[index], *before[:index], *before[index + 1:]]
        if after != expected:
            raise RuntimeError("Changing the boot default changed the running or saved deployments unexpectedly.")
    return "Rollback canceled."


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.rollback_cancel <64-character-token>", file=sys.stderr)
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
