"""Private, bounded persistence for maintenance request identities and outcomes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
from bazzite_portal.maintenance.logs import read_only_valid_unit


MAX_FILE_BYTES = 32 * 1024
MAX_RECORDS = 16
KINDS = {"cancel", "layers", "cache", "channel", "image", "delete", "rollback"}
STATES = {"running", "success", "failed", "unknown"}
_INVOCATION = re.compile(r"^[0-9a-fA-F]{32}$")
_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def current_boot_id(path="/proc/sys/kernel/random/boot_id"):
    """Return the kernel boot identity when the host exposes it."""
    try:
        with open(path, "r", encoding="ascii") as stream:
            value = stream.read(64).strip().lower()
    except (OSError, UnicodeError):
        return ""
    return value if _BOOT_ID.fullmatch(value) else ""


def _path(environ=None, home=None):
    environ = os.environ if environ is None else environ
    root = environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "bazzite-portal" / "maintenance.json"
    home = Path(home if home is not None else Path.home())
    return home / ".local" / "state" / "bazzite-portal" / "maintenance.json"


def _valid_record(kind, record):
    if kind not in KINDS or not isinstance(record, dict):
        return None
    unit = record.get("unit")
    state = record.get("state")
    invocation = record.get("invocation", "")
    generation = record.get("generation", 0)
    error = record.get("error", "")
    requested_at = record.get("requested_at", 0)
    if (not read_only_valid_unit(unit)
            or state not in STATES
            or not isinstance(invocation, str)
            or (invocation and not _INVOCATION.fullmatch(invocation))
            or type(generation) is not int or generation < 0
            or type(requested_at) is not int or requested_at < 0
            or not isinstance(error, str)):
        return None
    valid = {"unit": unit, "state": state, "invocation": invocation.lower(),
            "generation": generation, "requested_at": requested_at,
            "error": error[:1024]}
    if record.get("viewed") is True:
        valid["viewed"] = True
    return valid


def load(path=None, *, boot_id=None):
    """Load valid operation records, rejecting unsafe files and malformed data."""
    path = Path(path) if path is not None else _path()
    expected_boot = current_boot_id() if boot_id is None else str(boot_id).lower()
    if not _BOOT_ID.fullmatch(expected_boot):
        return {}
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return {}
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return {}
        if os.name == "posix" and info.st_mode & 0o077:
            return {}
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    if (not isinstance(data, dict) or set(data) != {"boot_id", "records"}
            or data.get("boot_id") != expected_boot or not isinstance(data["records"], dict)):
        return {}
    result = {}
    for kind, record in list(data["records"].items())[-MAX_RECORDS:]:
        valid = _valid_record(kind, record)
        if valid:
            result[kind] = valid
    return result


def save(records, path=None, *, boot_id=None):
    """Atomically store only validated identifiers and bounded request outcomes."""
    path = Path(path) if path is not None else _path()
    active_boot = current_boot_id() if boot_id is None else str(boot_id).lower()
    if not _BOOT_ID.fullmatch(active_boot):
        return
    valid = {}
    for kind, record in list(records.items())[-MAX_RECORDS:]:
        row = _valid_record(kind, record)
        if row:
            valid[kind] = row
    encoded = json.dumps({"boot_id": active_boot, "records": valid},
                         separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Maintenance state exceeded its size limit.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid"):
        parent = path.parent.lstat()
        if (parent.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode)
                or parent.st_mode & 0o077):
            raise PermissionError("Maintenance state directory is not private and user-owned.")
    fd, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
