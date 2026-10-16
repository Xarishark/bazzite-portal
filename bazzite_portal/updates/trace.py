"""Opt-in private trace for diagnosing UUPD parser behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
from datetime import datetime, timezone


_MAX_BYTES = 2 * 1024 * 1024
_MAX_LINE = 4096
_MAX_VALUE = 512
_LOCK = threading.Lock()


def _trace_path():
    override = os.environ.get("BAZZITE_PORTAL_UPDATE_TRACE_PATH")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    real_home = home.resolve()
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        state = Path(base).expanduser()
        # /home/<user> may be a trusted symlink to /var/home/<user> on Bazzite.
        # Normalize only the known home prefix; child components remain checked
        # by _private_directory so a symlink below the home is still rejected.
        if state.is_absolute():
            try:
                relative = state.relative_to(home)
            except ValueError:
                pass
            else:
                state = real_home / relative
    else:
        state = real_home / ".local" / "state"
    return state / "bazzite-portal" / "update-parser-trace.jsonl"


def _private_directory(path):
    """Create/check each parent without traversing a symlink."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("trace parent is not a real directory")
        if current == absolute.parent:
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise OSError("trace parent is owned by another user")
            os.chmod(current, 0o700)
    return absolute


def _bounded(value, limit=_MAX_VALUE):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    return None


def _progress_value(key, value):
    if key in ("planned", "completed") and isinstance(value, (list, tuple)):
        return [item[:_MAX_VALUE] for item in value[:64] if isinstance(item, str)]
    return _bounded(value)


def _module_snapshot(modules):
    rows = []
    if not isinstance(modules, (list, tuple)):
        return rows
    allowed = ("id", "status", "detail", "fraction", "error", "progress_item")
    for module in modules[:32]:
        if not isinstance(module, dict):
            continue
        row = {}
        for key in allowed:
            if key in module:
                row[key] = _bounded(module[key])
        progress = module.get("batch_progress")
        if isinstance(progress, dict):
            row["batch_progress"] = {
                key: _progress_value(key, progress.get(key))
                for key in ("total", "planned", "completed", "current", "item", "fraction")
                if key in progress
            }
        rows.append(row)
    return rows


def record(run, line, event, active, modules):
    """Append one bounded parser observation. All errors are intentionally swallowed."""
    if os.environ.get("BAZZITE_PORTAL_UPDATE_TRACE") != "1":
        return
    try:
        event_copy = {}
        if isinstance(event, dict):
            for key in ("kind", "id", "name", "detail", "error", "success", "fraction", "progress_percent"):
                if key in event:
                    event_copy[key] = _progress_value(key, event[key])
        result = "running"
        if event_copy.get("kind") == "completed":
            result = "success" if event_copy.get("success") is True else "failed"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run": _bounded(run, 128),
            "line": str(line)[:_MAX_LINE],
            "event": event_copy,
            "result": result,
            "active": _bounded(active, 128),
            "modules": _module_snapshot(modules),
        }
        payload = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", errors="replace")
        with _LOCK:
            path = _private_directory(_trace_path())
            _append(path, payload)
    except Exception:
        return


def _checked_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("trace path is not a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("trace file is owned by another user")
    os.chmod(path, 0o600)
    return True


def _append(path, payload):
    backup = path.with_name(path.name + ".1")
    exists = _checked_file(path)
    if exists and path.stat().st_size + len(payload) > _MAX_BYTES:
        _checked_file(backup)
        if backup.exists():
            backup.unlink()
        os.replace(path, backup)
        exists = False
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise OSError("unsafe trace file")
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
    finally:
        os.close(fd)
