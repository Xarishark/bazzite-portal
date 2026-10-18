"""Identity-checked removal of one staged rpm-ostree deployment."""

from __future__ import annotations
from bazzite_portal.core.i18n import C_, N_

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Callable


TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
LOCK_PATH = "/run/bazzite-portal-maintenance.lock"
ACTIVE_STATES = {"active", "activating", "deactivating", "reloading"}
OPERATION_UNITS = ("uupd.service", "uupd-manual.service", "bazzite-portal-maintenance@rollback.service",
                   "bazzite-portal-rollback.service")
MAX_STATUS_BYTES = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason[:1024], "token": "", "version": "Unknown", "image": "Unknown", "identity": "Unknown"}


def _clean(value: Any, fallback: str = "Unknown") -> str:
    if not isinstance(value, str):
        return fallback
    value = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
    return value[:1024] or fallback


def _has_transaction(value: Any) -> bool:
    if value is None or value is False or value == "" or value == [] or value == {}:
        return False
    return True


def _canonical_identity(item: dict[str, Any], osname: Any) -> tuple[str, str, str, str] | None:
    deployment_id = item.get("id")
    checksum = item.get("checksum")
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        return None
    if not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum):
        return None
    container_reference = item.get("container-image-reference", "")
    if container_reference is None:
        container_reference = ""
    if not isinstance(container_reference, str):
        return None
    osname = item.get("osname", osname)
    if osname is None:
        osname = ""
    if not isinstance(osname, str):
        return None
    return deployment_id.strip(), checksum.lower(), osname, container_reference


def _token(identity: tuple[str, str, str, str]) -> str:
    deployment_id, checksum, osname, container_reference = identity
    canonical = json.dumps(
        {"id": deployment_id, "checksum": checksum, "osname": osname,
         "container-image-reference": container_reference},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def target(data: Any) -> dict[str, Any]:
    """Describe the only rpm-ostree target this workflow may cancel."""
    if not isinstance(data, dict):
        return _unavailable("Deployment status is not an object.")
    if "deployments" not in data:
        if "status" in data:
            return _unavailable(C_("{bootc} deployments cannot be canceled by this workflow.", bootc="Bootc"))
        return _unavailable("RPM-OSTree deployment data is unavailable.")
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        return _unavailable("RPM-OSTree deployment list is missing or empty.")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("booted"), bool) for entry in deployments):
        return _unavailable("RPM-OSTree deployment list is malformed.")
    if sum(entry["booted"] for entry in deployments) != 1:
        return _unavailable("Expected exactly one running deployment.")
    if _has_transaction(data.get("transaction")):
        return _unavailable(C_('An {rpm_ostree} transaction is active.', rpm_ostree='RPM-OSTree'))

    pending = deployments[0]
    booted = next(entry for entry in deployments if entry["booted"] is True)
    pending_osname = pending.get("osname")
    booted_osname = booted.get("osname")
    if not isinstance(pending_osname, str) or not pending_osname.strip():
        return _unavailable("The pending deployment has no stateroot identity.")
    if not isinstance(booted_osname, str) or not booted_osname.strip():
        return _unavailable("The running deployment has no stateroot identity.")
    if pending_osname != booted_osname:
        return _unavailable("The pending deployment belongs to a different stateroot.")
    if pending["booted"] is not False:
        return _unavailable(N_("There is no pending deployment."))
    if pending.get("staged") is not True:
        return _unavailable(N_("The next boot is a scheduled rollback or other non-staged deployment."))
    if pending.get("pinned") is not False:
        return _unavailable(N_("The pending deployment is pinned or its pin state is unknown."))
    identity = _canonical_identity(pending, pending_osname)
    if identity is None:
        return _unavailable("The pending deployment identity is incomplete or malformed.")
    deployment_id, checksum, _osname, container_reference = identity
    return {
        "available": True,
        "reason": "",
        "token": _token(identity),
        "version": _clean(pending.get("version")),
        "image": _clean(container_reference),
        "identity": _clean(checksum),
    }


def _command(argv: list[str], timeout: float | None = 15) -> str:
    if timeout is None:
        return _stream_command(argv)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            argv,
            stdout=output,
            stderr=output,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        length = output.tell()
        output.seek(max(0, length - MAX_STATUS_BYTES))
        text = output.read().decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(text.strip() or f"{argv[0]} exited with {result.returncode}")
    return text


def _emit_journal_chunk(chunk: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(chunk)
        stream.flush()
    else:
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def _retain_bounded_tail(output, chunk: bytes) -> None:
    output.write(chunk)
    if output.tell() <= MAX_STATUS_BYTES:
        return
    output.seek(-MAX_STATUS_BYTES, os.SEEK_END)
    tail = output.read(MAX_STATUS_BYTES)
    output.seek(0)
    output.truncate()
    output.write(tail)
    output.seek(0, os.SEEK_END)


def _stream_command(argv: list[str]) -> str:
    """Stream an unbounded mutation's output to journald and retain only its tail."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "LC_ALL": "C"},
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read1(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                _emit_journal_chunk(chunk)
                _retain_bounded_tail(output, chunk)
            return_code = process.wait()
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
        output.seek(0)
        text = output.read().decode("utf-8", errors="replace")
    if return_code:
        raise RuntimeError(text.strip() or f"{argv[0]} exited with {return_code}")
    return text


@contextmanager
def _pending_lock(path: str = LOCK_PATH):
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("System maintenance lock file is not a root-owned mode-0600 regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another system maintenance operation is already running.") from exc
        yield
    finally:
        os.close(descriptor)


def _read_status(run_command: Callable[..., str]) -> dict[str, Any]:
    try:
        data = json.loads(run_command(["rpm-ostree", "status", "--json"], timeout=15))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RPM-OSTree returned malformed deployment status.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("RPM-OSTree returned malformed deployment status.")
    return data


def _active_operation(run_command: Callable[..., str]) -> str:
    for unit in OPERATION_UNITS:
        text = run_command(["systemctl", "show", unit, "--property=LoadState,ActiveState"], timeout=15)
        properties = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        load = properties.get("LoadState")
        active = properties.get("ActiveState")
        if load == "not-found":
            continue
        if load != "loaded" or not active:
            raise RuntimeError(f"Could not verify system operation state for {unit}.")
        if active in ACTIVE_STATES:
            return unit
    return ""


def _remaining_deployments(data: dict[str, Any], canceled_id: str, canceled_checksum: str = "") -> list[tuple[str, str, bool, bool]]:
    deployments = data.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        raise RuntimeError("RPM-OSTree deployment list is missing or empty.")
    remaining = []
    for entry in deployments:
        if not isinstance(entry, dict) or not isinstance(entry.get("booted"), bool):
            raise RuntimeError("RPM-OSTree deployment list is malformed.")
        deployment_id = entry.get("id")
        checksum = entry.get("checksum")
        if not isinstance(deployment_id, str) or not deployment_id or not isinstance(checksum, str) or not CHECKSUM_PATTERN.fullmatch(checksum):
            raise RuntimeError("A retained deployment has no stable identity.")
        if deployment_id == canceled_id and checksum.lower() == canceled_checksum.lower():
            continue
        remaining.append((deployment_id, checksum.lower(), entry["booted"], entry.get("pinned") is True))
    if sum(entry[2] for entry in remaining) != 1:
        raise RuntimeError("Expected exactly one retained running deployment.")
    return remaining


def execute(
    token: str,
    command_runner: Callable[..., str] = _command,
    lock_factory: Callable[[], Any] = _pending_lock,
) -> str:
    """Revalidate a confirmed token, remove only its pending deployment, verify."""
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid cancellation token.")
    with lock_factory():
        initial = _read_status(command_runner)
        initial_target = target(initial)
        if not initial_target["available"] or initial_target["token"] != token:
            raise RuntimeError(initial_target["reason"] or "The pending deployment changed after confirmation.")
        active = _active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot cancel a pending deployment while {active} is running.")

        # Re-read immediately before cleanup so a changed deployment or newly
        # started managed job cannot pass using the first snapshot.
        current = _read_status(command_runner)
        current_target = target(current)
        if not current_target["available"] or current_target["token"] != token:
            raise RuntimeError(current_target["reason"] or "The pending deployment changed before cleanup.")
        active = _active_operation(command_runner)
        if active:
            raise RuntimeError(f"Cannot cancel a pending deployment while {active} is running.")

        canceled_checksum = current_target["identity"]
        canceled_id = current["deployments"][0]["id"]
        retained_before = _remaining_deployments(current, canceled_id, canceled_checksum)
        if command_runner is _command:
            print("Removing the confirmed pending deployment...", flush=True)
        # cleanup --pending removes ALL unpinned deployments before the booted
        # one, including a saved deployment selected by an earlier rollback.
        # The confirmed staged target is index zero; remove only that entry.
        command_runner(["ostree", "admin", "undeploy", "0"], timeout=None)
        after = _read_status(command_runner)
        after_deployments = after.get("deployments")
        if not isinstance(after_deployments, list):
            raise RuntimeError("Could not verify deployments after cleanup.")
        if any(
            isinstance(entry, dict) and entry.get("id") == canceled_id
            and isinstance(entry.get("checksum"), str) and entry["checksum"].lower() == canceled_checksum.lower()
            for entry in after_deployments
        ):
            raise RuntimeError("The confirmed pending deployment is still present after cleanup.")
        retained_after = _remaining_deployments(after, canceled_id, canceled_checksum)
        if retained_after != retained_before:
            raise RuntimeError("Cleanup changed the running or saved deployments unexpectedly.")
    return "Pending deployment removed."


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not TOKEN_PATTERN.fullmatch(argv[0]):
        print("Usage: python3 -m bazzite_portal.maintenance.cancel_pending <64-character-token>", file=sys.stderr)
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
