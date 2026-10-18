"""Identity-checked deletion of one saved rpm-ostree deployment."""
from __future__ import annotations
from bazzite_portal.core.i18n import C_, N_

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
MAX_DEPLOYMENTS = 64
MAX_IDENTITY_BYTES = 128 * 1024


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "deployments": []}


def _identity(entry: dict[str, Any]) -> dict[str, Any] | None:
    deployment_id = entry.get("id")
    checksum = entry.get("checksum")
    osname = entry.get("osname")
    image = entry.get("container-image-reference", "")
    if (not isinstance(deployment_id, str) or not deployment_id.strip() or len(deployment_id) > 256
            or not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum)
            or not isinstance(osname, str) or not osname.strip() or len(osname) > 256
            or not isinstance(image, str) or len(image) > 2048):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for value in (deployment_id, osname, image) for char in value):
        return None
    return {"id": deployment_id.strip(), "checksum": checksum.lower(), "osname": osname,
            "container-image-reference": image}


def _snapshot(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | dict[str, Any]:
    if not isinstance(data, dict) or "deployments" not in data:
        return _unavailable("RPM-OSTree deployment data is unavailable.")
    entries = data.get("deployments")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_DEPLOYMENTS:
        return _unavailable("RPM-OSTree deployment list is missing, empty, or too large.")
    if any(not isinstance(item, dict) or not isinstance(item.get("booted"), bool) for item in entries):
        return _unavailable("RPM-OSTree deployment list is malformed.")
    if sum(item["booted"] for item in entries) != 1:
        return _unavailable("Expected exactly one booted deployment.")
    if data.get("transaction") not in (None, False, "", [], {}):
        return _unavailable(C_('An {rpm_ostree} transaction is active.', rpm_ostree='RPM-OSTree'))
    identities = []
    for item in entries:
        identity = _identity(item)
        if identity is None or not isinstance(item.get("pinned"), bool):
            return _unavailable("A deployment identity or pin state is incomplete.")
        identities.append({**identity, "booted": item["booted"], "pinned": item["pinned"],
                           "staged": item.get("staged") is True})
    # Index zero is the bootloader default. Keep the entire operation unavailable
    # while it is not the running deployment, including staged/rollback boots.
    if entries[0]["booted"] is not True or entries[0].get("staged") is True:
        return _unavailable(N_("Resolve the next-boot deployment before deleting saved deployments."))
    encoded = json.dumps(identities, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_IDENTITY_BYTES:
        return _unavailable("Deployment identity data is too large to confirm safely.")
    return identities, entries


def _snapshot_token(identities: list[dict[str, Any]]) -> str:
    encoded = json.dumps(identities, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def targets(data: Any) -> dict[str, Any]:
    """Return individually selectable saved deployments, excluding booted/default/staged."""
    snapshot = _snapshot(data)
    if isinstance(snapshot, dict):
        return snapshot
    identities, entries = snapshot
    whole_token = _snapshot_token(identities)
    selected = []
    for index, (entry, identity) in enumerate(zip(entries, identities)):
        if index == 0 or entry["booted"] or entry.get("staged") is True or entry["pinned"] is not True:
            continue
        material = json.dumps({"snapshot": whole_token, "index": index, "identity": identity,
                               "pinned": entry["pinned"]}, sort_keys=True, separators=(",", ":")).encode()
        token = hashlib.sha256(material).hexdigest()
        selected.append({"token": token, "index": index, "identity": identity["checksum"],
                         "version": _display(entry.get("version")),
                         "image": _display(identity["container-image-reference"]),
                         "pinned": entry["pinned"]})
    return {"available": bool(selected), "reason": "" if selected else N_('No saved, pinned deployments can be deleted.'),
            "deployments": selected}


def _display(value: Any) -> str:
    if not isinstance(value, str):
        return N_("Unknown")
    cleaned = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
    return cleaned[:512] or "Unknown"


def _read_status(run_command: Callable[..., str]) -> dict[str, Any]:
    try:
        data = json.loads(run_command(["rpm-ostree", "status", "--json"], timeout=15))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def _busy(run_command: Callable[..., str]) -> str:
    return cancel_pending._active_operation(run_command)


def _candidate_for_token(data: dict[str, Any], token: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
    result = targets(data)
    if not result["available"]:
        raise RuntimeError(result["reason"])
    match = next((item for item in result["deployments"] if item["token"] == token), None)
    if match is None:
        raise RuntimeError("The saved deployment list changed after confirmation.")
    index = match["index"]
    raw = data["deployments"][index]
    identity = _identity(raw)
    assert identity is not None
    return index, raw, identity


def _all_identities(data: dict[str, Any]) -> list[tuple[dict[str, Any], bool, bool]]:
    entries = data.get("deployments")
    if not isinstance(entries, list):
        raise RuntimeError("Could not verify deployments after deletion.")
    result = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("booted"), bool):
            raise RuntimeError("RPM-OSTree deployment list is malformed.")
        identity = _identity(item)
        if identity is None or not isinstance(item.get("pinned"), bool):
            raise RuntimeError("A retained deployment has no stable identity.")
        result.append((identity, item["booted"], item["pinned"]))
    if sum(booted for _identity_value, booted, _pinned in result) != 1:
        raise RuntimeError("Expected exactly one retained running deployment.")
    return result


def execute(token: str, command_runner: Callable[..., str] = cancel_pending._command,
            lock_factory: Callable[[], Any] = cancel_pending._pending_lock) -> str:
    """Revalidate one displayed target, unpin if necessary, then undeploy and verify."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid saved-deployment deletion token.")
    with lock_factory():
        initial = _read_status(command_runner)
        _candidate_for_token(initial, token)
        active = _busy(command_runner)
        if active:
            raise RuntimeError(f"Cannot delete a saved deployment while {active} is running.")
        current = _read_status(command_runner)
        index, entry, identity = _candidate_for_token(current, token)
        active = _busy(command_runner)
        if active:
            raise RuntimeError(f"Cannot delete a saved deployment while {active} is running.")
        retained_before = _all_identities(current)
        # Refuse ambiguous IDs before a possible unpin so even the preliminary
        # mutation cannot affect a deployment we cannot identify uniquely.
        _find_identity(current, identity)
        if entry["pinned"]:
            command_runner(["ostree", "admin", "pin", "--unpin", str(index)], timeout=None)
            after_unpin = _read_status(command_runner)
            new_index, new_entry, new_identity = _find_identity(after_unpin, identity)
            if new_entry.get("pinned") is not False or new_index != index:
                raise RuntimeError("Could not safely unpin the confirmed deployment.")
            expected_unpinned = [(saved_identity, booted,
                                  False if saved_identity == identity else pinned)
                                 for saved_identity, booted, pinned in retained_before]
            if _all_identities(after_unpin) != expected_unpinned:
                raise RuntimeError("Unpinning changed another deployment unexpectedly.")
            current, index, entry = after_unpin, new_index, new_entry
        # Index is derived from the freshly read status immediately before use.
        new_index, new_entry, new_identity = _find_identity(current, identity)
        if new_index != index or new_entry["booted"] or new_entry.get("staged") is True:
            raise RuntimeError("The selected deployment is no longer safe to delete.")
        try:
            command_runner(["ostree", "admin", "undeploy", str(new_index)], timeout=None)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as undeploy_error:
            if entry.get("pinned") is False:
                try:
                    failure_state = _read_status(command_runner)
                    failure_index, failure_entry, _failure_identity = _find_identity(failure_state, identity)
                    if failure_entry.get("pinned") is False:
                        command_runner(["ostree", "admin", "pin", str(failure_index)], timeout=None)
                        restored = _read_status(command_runner)
                        _restored_index, restored_entry, _restored_identity = _find_identity(restored, identity)
                        if restored_entry.get("pinned") is not True:
                            raise RuntimeError("OSTree did not restore the deployment pin.")
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as restore_error:
                    raise RuntimeError(
                        f"Could not delete the saved deployment and could not verify its pin restoration: {restore_error}"
                    ) from undeploy_error
            raise RuntimeError(f"Could not delete the saved deployment: {undeploy_error}") from undeploy_error
        after = _read_status(command_runner)
        retained_after = _all_identities(after)
        expected = [(item, booted, pinned) for item, booted, pinned in retained_before
                    if item != identity]
        # The selected deployment's pin state was intentionally cleared first.
        if entry.get("pinned") is False:
            expected = [(item, booted, False if item == identity else pinned)
                        for item, booted, pinned in expected]
        if retained_after != expected:
            raise RuntimeError("Deletion changed the running or saved deployments unexpectedly.")
    return "Saved deployment deleted."


def _find_identity(data: dict[str, Any], identity: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
    safe = _snapshot(data)
    if isinstance(safe, dict):
        raise RuntimeError(safe["reason"])
    entries = data.get("deployments")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_DEPLOYMENTS:
        raise RuntimeError("Could not re-read deployments safely.")
    matches = [(index, entry, _identity(entry)) for index, entry in enumerate(entries)
               if isinstance(entry, dict) and _identity(entry) == identity]
    if len(matches) != 1:
        raise RuntimeError("The selected deployment identity is missing or ambiguous.")
    index, entry, found = matches[0]
    if index == 0 or entry["booted"] or entry.get("staged") is True:
        raise RuntimeError("The selected deployment is no longer a saved deployment.")
    assert found is not None
    return index, entry, found


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.delete_deployments <64-character-token>", file=sys.stderr)
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
