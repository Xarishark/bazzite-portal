"""Persistent per-user acknowledgements for one-time application flows."""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from pathlib import Path

LOGGER = logging.getLogger("bazzite_portal")
MAX_MARKER_BYTES = 256
_MARKER_CONTENT = b'{"version":1,"completed":true}\n'


class OnceMarker:
    """Track whether a one-time flow was handled.

    ``base`` is a test/profile XDG state root. Production uses XDG_STATE_HOME
    or ``~/.local/state``. Unsafe or unreadable state fails closed: the caller
    treats the one-time flow as already completed, avoiding repeated launches.
    """

    def __init__(self, name: str, base: str | os.PathLike[str] | None = None) -> None:
        if not name or Path(name).name != name or "\\" in name or name in {".", ".."}:
            raise ValueError("A marker must be a plain filename.")
        if base is None:
            state_home = os.environ.get("XDG_STATE_HOME")
            self._root = Path(state_home) if state_home else Path.home() / ".local" / "state"
        else:
            self._root = Path(base)
        self._directory = self._root / "bazzite-portal"
        self._marker = self._directory / name

    def completed(self) -> bool:
        """Return whether the one-time flow should be suppressed.

        Missing state permits the one-time launch. Present but malformed or
        unsafe state fails closed so a damaged marker cannot relaunch forever.
        """
        try:
            try:
                directory_info = self._directory.lstat()
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(directory_info.st_mode):
                LOGGER.warning("One-time state directory is unsafe; suppressing automatic flow")
                return True
            if _not_owned(directory_info):
                LOGGER.warning("One-time state directory belongs to another user; suppressing automatic flow")
                return True
            info = self._marker.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MARKER_BYTES or _not_owned(info):
                LOGGER.warning("One-time marker is unsafe; suppressing automatic flow")
                return True
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self._marker, flags)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_MARKER_BYTES or _not_owned(opened):
                    LOGGER.warning("One-time marker changed while opening; suppressing automatic flow")
                    return True
                data = os.read(fd, MAX_MARKER_BYTES + 1)
            finally:
                os.close(fd)
            try:
                payload = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                LOGGER.warning("One-time marker is malformed; suppressing automatic flow")
                return True
            if not (isinstance(payload, dict) and payload.get("version") == 1 and payload.get("completed") is True):
                LOGGER.warning("One-time marker has an unknown format; suppressing automatic flow")
                return True
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            LOGGER.warning("Cannot safely read one-time marker; suppressing automatic flow: %s", exc)
            return True

    def acknowledge(self) -> bool:
        """Atomically record that the flow was handled.

        Returns false on an unsafe or failed write; callers can continue normal
        operation because this marker only suppresses an automatic setup flow.
        """
        temporary: Path | None = None
        try:
            if not self._ensure_directory():
                return False
            try:
                current = self._marker.lstat()
            except FileNotFoundError:
                current = None
            if current is not None:
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_size > MAX_MARKER_BYTES or _not_owned(current):
                    LOGGER.warning("Cannot acknowledge one-time flow: marker path is unsafe")
                    return False
                if self.completed():
                    return True

            temporary = self._directory / ("." + self._marker.stem + "-" + secrets.token_hex(8) + ".tmp")
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
            LOGGER.warning("Cannot acknowledge one-time flow: %s", exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _ensure_directory(self) -> bool:
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = self._directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                LOGGER.warning("One-time state directory is unsafe; suppressing automatic flow")
                return False
            if os.name == "posix" and _not_owned(info):
                LOGGER.warning("One-time state directory belongs to another user")
                return False
            if os.name == "posix":
                os.chmod(self._directory, 0o700)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            LOGGER.warning("Cannot access one-time state directory: %s", exc)
            return False


def _not_owned(info: os.stat_result) -> bool:
    return os.name == "posix" and hasattr(os, "getuid") and info.st_uid != os.getuid()
