"""Shared command, streaming, and lock helpers for privileged maintenance."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import re


CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


LOCK_PATH = "/run/bazzite-portal-maintenance.lock"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
ACTIVE_STATES = frozenset(("active", "activating", "deactivating", "reloading"))
OPERATION_UNITS = (
    "uupd.service", "uupd-manual.service",
    "bazzite-portal-maintenance@rollback.service",
    "bazzite-portal-rollback.service",
)


def command(argv: list[str], timeout: float | None = 15) -> str:
    """Run a command with C locale and return its bounded output or tail."""
    if timeout is None:
        return stream_command(argv)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            argv, stdout=output, stderr=output, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"}, check=False,
        )
        length = output.tell()
        output.seek(max(0, length - MAX_OUTPUT_BYTES))
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
    if output.tell() <= MAX_OUTPUT_BYTES:
        return
    output.seek(-MAX_OUTPUT_BYTES, os.SEEK_END)
    tail = output.read(MAX_OUTPUT_BYTES)
    output.seek(0)
    output.truncate()
    output.write(tail)
    output.seek(0, os.SEEK_END)


def stream_command(argv: list[str]) -> str:
    """Stream unbounded mutation output to journald and retain a bounded tail."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env={**os.environ, "LC_ALL": "C"},
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
def maintenance_lock(path: str = LOCK_PATH):
    """Acquire the root-owned, nonblocking system maintenance lock."""
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RuntimeError("System maintenance lock file is not a root-owned mode-0600 regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another system maintenance operation is already running.") from exc
        yield
    finally:
        os.close(descriptor)


def active_operation(run_command) -> str:
    """Return an active managed operation, failing closed on unknown state."""
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


def has_transaction(value) -> bool:
    return value is not None and value is not False and value != "" and value != [] and value != {}


def clean_text(value, fallback: str = "Unknown") -> str:
    """Sanitize and bound a human-readable deployment field."""
    if not isinstance(value, str):
        return fallback
    value = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
    return value[:1024] or fallback


def canonical_deployment_identity(item, osname):
    """Return the normalized stable fields shared by deployment verifiers."""
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
