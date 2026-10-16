"""Persistent acknowledgement for the packaged app's first graphical login."""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from pathlib import Path

LOGGER = logging.getLogger("bazzite_portal")
MAX_MARKER_BYTES = 256
_MARKER_NAME = "first-login.json"
_MARKER_CONTENT = b'{"version":1,"completed":true}\n'


class FirstLogin:
    """Track whether the first-login window was successfully presented.

    ``base`` is a test/profile XDG state root. Production uses XDG_STATE_HOME
    or ``~/.local/state``. Unsafe or unreadable state fails closed: the caller
    treats the first login as already completed, avoiding repeated launches.
    """

    def __init__(self, base: str | os.PathLike[str] | None = None) -> None:
        if base is None:
            state_home = os.environ.get("XDG_STATE_HOME")
            self._root = Path(state_home) if state_home else Path.home() / ".local" / "state"
        else:
            self._root = Path(base)
        self._directory = self._root / "bazzite-portal"
        self._marker = self._directory / _MARKER_NAME

    def completed(self) -> bool:
        """Return whether first-login launch should be suppressed.

        Missing state permits the one-time launch. Present but malformed or
        unsafe state fails closed so a damaged marker cannot relaunch forever.
        """
        try:
            try:
                directory_info = self._directory.lstat()
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(directory_info.st_mode):
                LOGGER.warning("First-login state directory is unsafe; suppressing automatic launch")
                return True
            if _not_owned(directory_info):
                LOGGER.warning("First-login state directory belongs to another user; suppressing automatic launch")
                return True
            info = self._marker.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MARKER_BYTES or _not_owned(info):
                LOGGER.warning("First-login marker is unsafe; suppressing automatic launch")
                return True
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self._marker, flags)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_MARKER_BYTES or _not_owned(opened):
                    LOGGER.warning("First-login marker changed while opening; suppressing automatic launch")
                    return True
                data = os.read(fd, MAX_MARKER_BYTES + 1)
            finally:
                os.close(fd)
            try:
                payload = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                LOGGER.warning("First-login marker is malformed; suppressing automatic launch")
                return True
            if not (isinstance(payload, dict) and payload.get("version") == 1 and payload.get("completed") is True):
                LOGGER.warning("First-login marker has an unknown format; suppressing automatic launch")
                return True
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            LOGGER.warning("Cannot safely read first-login marker; suppressing automatic launch: %s", exc)
            return True

    def acknowledge(self) -> bool:
        """Atomically record success after the GUI presents its window.

        Returns false on an unsafe or failed write; callers can continue normal
        startup because this marker is only an optional launch convenience.
        """
        temporary: Path | None = None
        try:
            if not self._ensure_directory(create=True):
                return False
            try:
                current = self._marker.lstat()
            except FileNotFoundError:
                current = None
            if current is not None:
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_size > MAX_MARKER_BYTES or _not_owned(current):
                    LOGGER.warning("Cannot acknowledge first login: marker path is unsafe")
                    return False
                if self.completed():
                    return True

            temporary = self._directory / (".first-login-" + secrets.token_hex(8) + ".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(temporary, flags, 0o600)
            try:
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(_MARKER_CONTENT)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self._marker)
            temporary = None
            if os.name == "posix":
                dir_fd = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return True
        except OSError as exc:
            LOGGER.warning("Cannot acknowledge first login: %s", exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _ensure_directory(self, *, create: bool) -> bool:
        try:
            if create:
                self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = self._directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                LOGGER.warning("First-login state directory is unsafe; suppressing automatic launch")
                return False
            if os.name == "posix" and _not_owned(info):
                LOGGER.warning("First-login state directory belongs to another user")
                return False
            if os.name == "posix":
                os.chmod(self._directory, 0o700)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            LOGGER.warning("Cannot access first-login state directory: %s", exc)
            return False


def _not_owned(info: os.stat_result) -> bool:
    return os.name == "posix" and hasattr(os, "getuid") and info.st_uid != os.getuid()
