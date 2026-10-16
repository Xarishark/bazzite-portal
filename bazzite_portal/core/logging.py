"""Private, bounded application logging for the Bazzite Portal.

Callers decide what is safe to log. Never pass passwords, environment values,
or user-entered content to this logger.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
import time
import traceback
from pathlib import Path

APP_LOGGER_NAME = "bazzite_portal"
LOG_FILENAME = "bazzite-portal.log"
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 2
MAX_ENTRY_BYTES = 16 * 1024

_handler: "_BoundedFileHandler | None" = None
_handler_lock = threading.Lock()
_hooks_installed = False
_original_excepthook = sys.excepthook
_original_threading_excepthook = getattr(threading, "excepthook", None)


def get_log_directory(base: str | os.PathLike[str] | None = None) -> Path:
    """Return the private log directory, creating it with owner-only access.

    ``base`` is a test/profile root; production uses XDG_STATE_HOME or
    ``~/.local/state``.
    """
    if base is None:
        state_home = os.environ.get("XDG_STATE_HOME")
        root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    else:
        root = Path(base)
    app_directory = root / "bazzite-portal"
    app_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_private_directory(app_directory)
    directory = app_directory / "logs"
    directory.mkdir(exist_ok=True, mode=0o700)
    _ensure_private_directory(directory)
    return directory


def log_paths(base: str | os.PathLike[str] | None = None) -> list[Path]:
    """Return existing active and backup logs that are regular, non-symlink files."""
    directory = get_log_directory(base)
    paths = [directory / LOG_FILENAME]
    paths.extend(directory / f"{LOG_FILENAME}.{i}" for i in range(1, BACKUP_COUNT + 1))
    safe: list[Path] = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            safe.append(path)
    return safe


def setup_logging(role: str = "app", base: str | os.PathLike[str] | None = None) -> logging.Logger:
    """Configure the package logger once and install uncaught-exception hooks.

    ``role`` identifies this process (for example ``gui`` or ``agent``). The
    package logger is isolated from root logging configuration.
    """
    global _handler, _hooks_installed
    with _handler_lock:
        logger = logging.getLogger(APP_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if _handler is None:
            try:
                _handler = _BoundedFileHandler(get_log_directory(base), role)
            except Exception:
                # Logging must never prevent application startup.
                return logger
            logger.addHandler(_handler)
        else:
            _handler.set_role(role)
        if not _hooks_installed:
            sys.excepthook = _sys_exception_hook
            if hasattr(threading, "excepthook"):
                threading.excepthook = _thread_exception_hook
            _hooks_installed = True
        return logger


class _BoundedFileHandler(logging.Handler):
    def __init__(self, directory: Path, role: str):
        super().__init__()
        self.directory = directory
        self.path = directory / LOG_FILENAME
        self.lock_path = directory / f"{LOG_FILENAME}.lock"
        self.role = role.replace("\n", " ")[:40]
        self._emit_lock = threading.RLock()

    def set_role(self, role: str) -> None:
        with self._emit_lock:
            self.role = str(role).replace("\n", " ")[:40]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            line = _format_entry(record, self.role, message)
            with self._emit_lock:
                self._with_file_lock(lambda: self._write_line(line))
        except Exception:
            # Diagnostics are best-effort; never recurse through logging here.
            return

    def _with_file_lock(self, callback) -> None:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            _validate_private_file(fd)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r+b", closefd=False) as lock_file:
                _lock_file(lock_file)
                try:
                    callback()
                finally:
                    _unlock_file(lock_file)
        finally:
            os.close(fd)

    def _write_line(self, line: bytes) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > MAX_LOG_BYTES:
            # Do not carry an unexpectedly oversized prior file into backups.
            self.path.unlink()
            size = 0
        if size and size + len(line) > MAX_LOG_BYTES:
            self._rotate()
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            _validate_private_file(fd)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "ab", closefd=False) as output:
                output.write(line)
        finally:
            os.close(fd)

    def _rotate(self) -> None:
        oldest = self.directory / f"{LOG_FILENAME}.{BACKUP_COUNT}"
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for index in range(BACKUP_COUNT - 1, 0, -1):
            source = self.directory / f"{LOG_FILENAME}.{index}"
            destination = self.directory / f"{LOG_FILENAME}.{index + 1}"
            try:
                if source.stat().st_size > MAX_LOG_BYTES:
                    source.unlink()
                    continue
                os.replace(source, destination)
            except FileNotFoundError:
                pass
        try:
            if self.path.stat().st_size > MAX_LOG_BYTES:
                self.path.unlink()
                return
            os.replace(self.path, self.directory / f"{LOG_FILENAME}.1")
        except FileNotFoundError:
            pass


def _format_entry(record: logging.LogRecord, role: str, message: str) -> bytes:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
    prefix = f"{timestamp} {role} pid={os.getpid()} {record.levelname}: "
    # Keep each entry a single physical line and enforce the byte bound even
    # when the message contains multibyte characters.
    message = message.replace("\r", "\\r").replace("\n", "\\n")
    payload = (prefix + message).encode("utf-8", errors="replace")
    payload = payload[: MAX_ENTRY_BYTES - 1].decode("utf-8", errors="ignore").encode("utf-8")
    return payload + b"\n"


def _ensure_private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"log path is not a directory: {path}")
    if os.name == "posix":
        os.chmod(path, 0o700)


def _validate_private_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("refusing non-regular or linked application log file")
    if os.name == "posix" and info.st_uid != os.getuid():
        raise OSError("refusing application log file owned by another user")


def _lock_file(file_obj) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        if os.fstat(file_obj.fileno()).st_size == 0:
            file_obj.write(b"0")
            file_obj.flush()
        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _unlock_file(file_obj) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


def _sys_exception_hook(exc_type, exc_value, exc_traceback) -> None:
    logger = logging.getLogger(APP_LOGGER_NAME)
    if logger.handlers:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    _original_excepthook(exc_type, exc_value, exc_traceback)


def _thread_exception_hook(args) -> None:
    logger = logging.getLogger(APP_LOGGER_NAME)
    if logger.handlers:
        logger.critical("Uncaught thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    if _original_threading_excepthook is not None:
        _original_threading_excepthook(args)
