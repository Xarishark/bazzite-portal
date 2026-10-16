"""Identity-bound Bazzite stable/testing channel switch targets."""

from __future__ import annotations
from bazzite_portal.core.i18n import N_

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any

from bazzite_portal.maintenance import cancel_pending

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_STATUS_BYTES = 2 * 1024 * 1024
_REFERENCE = re.compile(
    r"^(?:(?:ostree-image-signed|ostree-unverified-image):docker://|"
    r"ostree-unverified-registry:)"
    r"(?P<repository>ghcr\.io/ublue-os/(?P<image>bazzite(?:-[a-z0-9][a-z0-9._-]*)?))"
    # Official GHCR tags include the moving track tags (stable/testing/unstable), as
    # well as immutable release tags such as stable-44.20261012 and
    # testing-44.20261012.2. Switching from either form targets the moving tag
    # for the same track.
    r":(?P<branch>stable|testing|unstable)(?:-(?P<version>\d+(?:\.\d+)*))?$"
)
_EQUAL_REBASE_LINE = re.compile(r"(?:error: )?Old and new refs are equal: (\S+)")


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "",
            "current_track": "", "target_track": "", "image": "",
            "target_reference": "", "version": "Unknown", "identity": ""}


def _token(identity: dict[str, str], target: str) -> str:
    raw = json.dumps({"identity": identity, "target": target}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def target(data: Any, *, helper_available: bool = True) -> dict[str, Any]:
    """Describe a safe switch from the booted image to the other/latest track."""
    if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
        return _unavailable("RPM-OSTree deployment status is unavailable.")
    deployments = data["deployments"]
    if not deployments or len(deployments) > 64:
        return _unavailable("RPM-OSTree deployment list is empty or too large.")
    if data.get("transaction") not in (None, False, "", [], {}):
        return _unavailable(N_("An RPM-OSTree transaction is active."))
    booted = [item for item in deployments if isinstance(item, dict) and item.get("booted") is True]
    if len(booted) != 1 or any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool)
                               for item in deployments):
        return _unavailable("The running deployment could not be identified safely.")
    running = booted[0]
    reference = running.get("container-image-reference")
    if not isinstance(reference, str) or len(reference) > 1024:
        return _unavailable("The running image reference is missing or malformed.")
    match = _REFERENCE.fullmatch(reference)
    if not match:
        return _unavailable(N_("Only official Bazzite stable/testing track and release references are supported."))
    deployment_id, checksum, osname = running.get("id"), running.get("checksum"), running.get("osname")
    if (not isinstance(deployment_id, str) or not deployment_id.strip() or len(deployment_id) > 256
            or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum)
            or not isinstance(osname, str) or not osname.strip() or len(osname) > 128):
        return _unavailable("The running deployment identity is incomplete.")
    branch = match.group("branch")
    # Stable/testing release tags return to the moving tag on the same track.
    # Unstable has no stable/testing counterpart, so its escape target is stable.
    # Bare stable/testing references keep their existing opposite-track behavior.
    if branch == "unstable":
        target_branch = "stable"
    elif match.group("version"):
        target_branch = branch
    else:
        target_branch = "testing" if branch == "stable" else "stable"
    target_reference = f"ostree-image-signed:docker://{match.group('repository')}:{target_branch}"
    identity = {"id": deployment_id, "checksum": checksum.lower(), "osname": osname,
                "reference": reference}
    result = {"available": True, "reason": "", "token": _token(identity, target_branch),
            "current_track": branch, "target_track": target_branch,
            "image": match.group("image"), "version": str(running.get("version") or "Unknown")[:256],
            "target_reference": target_reference,
            "identity": _token(identity, "identity")}
    if any(index == 0 and item.get("booted") is False or
           item.get("booted") is False and item.get("staged") is True
           for index, item in enumerate(deployments)):
        result.update(available=False,
                      reason=N_("Resolve the pending deployment before changing update channels."))
    elif not helper_available:
        result.update(available=False, reason="The channel-switch service is not installed.")
    return result


def verify_token(data: Any, token: str, *, helper_available: bool = True) -> dict[str, Any]:
    """Recompute the switch target at execution time and reject stale confirmations."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid channel-switch confirmation token.")
    current = target(data, helper_available=helper_available)
    if not current["available"] or current["token"] != token:
        raise RuntimeError(current["reason"] or "The running deployment changed after confirmation.")
    return current


def verify_switch_result(before: dict[str, Any], after: Any, reference: str) -> None:
    """Confirm RPM-OSTree staged the requested reference without changing the booted OS."""
    error = "The switch command finished, but the requested deployment could not be verified."
    if not isinstance(after, dict) or cancel_pending._has_transaction(after.get("transaction")):
        raise RuntimeError(error)
    deployments = after.get("deployments")
    if (not isinstance(deployments, list) or not 2 <= len(deployments) <= 64
            or any(not isinstance(item, dict) or type(item.get("booted")) is not bool
                   for item in deployments)):
        raise RuntimeError(error)
    running = [item for item in deployments if item["booted"]]
    original = next(item for item in before["deployments"] if item["booted"])
    if len(running) != 1:
        raise RuntimeError(error)
    identity = cancel_pending._canonical_identity(original, original["osname"])
    if (cancel_pending._canonical_identity(running[0], None) != identity
            or running[0].get("staged") is True):
        raise RuntimeError(error)
    pending = deployments[0]
    if (pending["booted"] or pending.get("staged") is not True
            or pending.get("osname") != original["osname"]
            or pending.get("container-image-reference") != reference
            or cancel_pending._canonical_identity(pending, None) is None
            or any(item.get("staged") is True for item in deployments[1:])):
        raise RuntimeError(error)


def _target_reference_is_booted(data: Any, reference: str) -> bool:
    deployments = data.get("deployments") if isinstance(data, dict) else None
    if not isinstance(deployments, list) or len(deployments) > 64:
        return False
    booted = [entry for entry in deployments
              if isinstance(entry, dict) and entry.get("booted") is True]
    return (len(booted) == 1 and booted[0].get("container-image-reference") == reference)


def _rebase(run_command, reference: str) -> None:
    try:
        run_command(["rpm-ostree", "rebase", reference], timeout=None)
    except RuntimeError as error:
        equal_reference = any(
            (match := _EQUAL_REBASE_LINE.fullmatch(line)) is not None
            and match.group(1) == reference
            for line in str(error).splitlines()
        )
        if not equal_reference:
            raise
        try:
            already_current = _target_reference_is_booted(_read_status(run_command), reference)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            already_current = False
        if already_current:
            raise RuntimeError(N_("You’re already using this image and channel. Use Update to get the latest version.")) from error
        raise


def execute(token: str, run_command=cancel_pending._command,
            lock_factory=cancel_pending._pending_lock) -> str:
    """Revalidate and ask RPM-OSTree to stage the latest image on the target track."""
    with lock_factory():
        data = _read_status(run_command)
        current = verify_token(data, token)
        active = cancel_pending._active_operation(run_command)
        if active:
            raise RuntimeError(f"Cannot change update channels while {active} is running.")
        # Re-read after the operation inventory and bind authorization to that
        # fresh state immediately before staging the selected signed reference.
        data = _read_status(run_command)
        current = verify_token(data, token)
        active = cancel_pending._active_operation(run_command)
        if active:
            raise RuntimeError(f"Cannot change update channels while {active} is running.")
        _rebase(run_command, current["target_reference"])
        verify_switch_result(data, _read_status(run_command), current["target_reference"])
    return f"Bazzite is switching to the {current['target_track']} channel. Restart to apply the staged deployment."


def _read_status(run_command) -> dict[str, Any]:
    try:
        raw = run_command(["rpm-ostree", "status", "--json"], timeout=15)
        if not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > MAX_STATUS_BYTES:
            raise ValueError("oversized status")
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from error
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.channel_switch <64-character-token>", file=sys.stderr)
        return 2
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("This helper must run as root.", file=sys.stderr)
        return 1
    try:
        print(execute(argv[0]))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
