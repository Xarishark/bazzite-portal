"""Safely reset disposable top-level entries in the current user's Steam folder.

Call :func:`reset_steam` from a worker thread. This module has no GTK or Steam
launcher dependency and never starts Steam.
"""

from __future__ import annotations

import os
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path
import subprocess


PRESERVED_ENTRIES = frozenset({
    "userdata", "compatibilitytools.d", "config", "controller_base",
    "steamapps", "music",
})
PRESERVED_MARKERS = tuple(sorted(PRESERVED_ENTRIES, key=len, reverse=True))
PROCESS_STOP_TIMEOUT = 5.0
PROCESS_POLL_INTERVAL = 0.05
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class ResetResult:
    removed: tuple[str, ...]
    preserved: tuple[str, ...]


class SteamResetError(RuntimeError):
    """Actionable failure, including entries already removed on partial failure."""

    def __init__(self, message: str, removed: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.removed = removed


class _ProcProcesses:
    """Small /proc-based equivalent of killall -9 steam, scoped to this UID."""

    def __init__(self, proc_root: Path = Path("/proc"), uid: int | None = None,
                 signal_process=None) -> None:
        self.proc_root = Path(proc_root)
        self.uid = os.getuid() if uid is None else uid
        self.signal_process = signal_process or self._signal_process

    @staticmethod
    def _signal_process(pid: int) -> None:
        pidfd = None
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                pidfd = os.pidfd_open(pid)
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        finally:
            if pidfd is not None:
                os.close(pidfd)

    def matching(self) -> list[tuple[int, str]]:
        matches: list[tuple[int, str]] = []
        try:
            entries = os.scandir(self.proc_root)
        except OSError as exc:
            raise SteamResetError(f"Cannot inspect processes before resetting Steam: {exc}") from exc
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(self.proc_root / entry.name / "comm", encoding="utf-8") as stream:
                        name = stream.read().strip()
                    if name != "steam":
                        continue
                    with open(self.proc_root / entry.name / "status", encoding="ascii") as stream:
                        status = stream.read()
                    process_uid = next(
                        int(line.split()[1]) for line in status.splitlines()
                        if line.startswith("Uid:")
                    )
                    state = next(
                        line.split()[1] for line in status.splitlines()
                        if line.startswith("State:")
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, StopIteration, ValueError) as exc:
                    raise SteamResetError(f"Cannot verify Steam process {entry.name}: {exc}") from exc
                if process_uid == self.uid and state != "Z":
                    matches.append((int(entry.name), state))
        return matches

    def stop_and_wait(self) -> None:
        initial = self.matching()
        for pid, _state in initial:
            try:
                self.signal_process(pid)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise SteamResetError(f"Cannot stop Steam process {pid}: {exc}") from exc
        deadline = time.monotonic() + PROCESS_STOP_TIMEOUT
        while True:
            remaining = self.matching()
            if not remaining:
                return
            if time.monotonic() >= deadline:
                pids = ", ".join(str(pid) for pid, _ in remaining)
                raise SteamResetError(f"Steam processes did not stop: {pids}")
            time.sleep(PROCESS_POLL_INTERVAL)


def _safe_root(home: Path) -> tuple[Path, int]:
    home = Path(home)
    if not home.is_absolute():
        raise SteamResetError("Cannot reset Steam: the home directory must be absolute.")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SteamResetError("Cannot safely reset Steam on this platform: safe directory operations are unavailable.")
    try:
        home = home.resolve(strict=True)
        if home.stat().st_uid != os.getuid():
            raise SteamResetError(f"Cannot reset Steam: home directory is not owned by the current user: {home}")
    except SteamResetError:
        raise
    except OSError as exc:
        raise SteamResetError(f"Cannot safely resolve the current user's home directory: {exc}") from exc
    # This is the only permitted target. Resolve the trusted home alias first,
    # then reject symlinks in every Steam-folder component.
    root = home / ".local" / "share" / "Steam"
    try:
        current_fd = os.open(root.anchor, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise SteamResetError(f"Cannot safely open Steam folder path {root}: {exc}") from exc
    current = Path(root.anchor)
    try:
        for component in root.parts[1:]:
            current = current / component
            try:
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                if current.is_symlink():
                    raise SteamResetError(f"Steam folder contains an unsafe symbolic link: {current}") from exc
                raise SteamResetError(f"Steam folder is unavailable at {root}: {exc}") from exc
            os.close(current_fd)
            current_fd = child_fd
        try:
            info = os.fstat(current_fd)
        except OSError as exc:
            raise SteamResetError(f"Steam folder is unavailable at {root}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise SteamResetError(f"Steam folder is not a directory: {root}")
        if info.st_uid != os.getuid():
            raise SteamResetError(f"Cannot reset Steam: folder is not owned by the current user: {root}")
        return root, current_fd
    except Exception:
        os.close(current_fd)
        raise


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one child without following symlinks, even below the root."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            with os.scandir(child_fd) as children:
                child_names = [child.name for child in children]
            for child_name in child_names:
                _remove_tree_at(child_fd, child_name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        # Includes symlinks: unlink the link itself, never its target.
        os.unlink(name, dir_fd=parent_fd)


def reset_steam(
    home: str | os.PathLike[str] | None = None,
    process_control: object | None = None,
) -> ResetResult:
    """Stop this user's Steam processes and clear disposable top-level files.

    ``home`` is injectable for isolated tests; production callers should omit
    it. ``process_control`` supports a test double exposing ``stop_and_wait``.
    """
    if os.name == "posix" and os.geteuid() == 0:
        raise SteamResetError("Cannot reset Steam as root; run the app as the Steam folder's user.")
    selected_home = Path(home) if home is not None else Path.home()
    root, root_fd = _safe_root(selected_home)
    try:
        try:
            names = sorted(name for name in os.listdir(root_fd) if not name.startswith("."))
        except OSError as exc:
            raise SteamResetError(f"Cannot list Steam folder {root}: {exc}") from exc
        preserved = tuple(name for name in names if any(marker in name for marker in PRESERVED_MARKERS))
        disposable = [name for name in names if not any(marker in name for marker in PRESERVED_MARKERS)]
        processes = process_control if process_control is not None else _ProcProcesses()
        try:
            processes.stop_and_wait()  # type: ignore[attr-defined]
        except SteamResetError:
            raise
        except Exception as exc:
            raise SteamResetError(f"Could not stop Steam before resetting: {exc}") from exc
        removed: list[str] = []
        for name in disposable:
            try:
                _remove_tree_at(root_fd, name)
                removed.append(name)
            except FileNotFoundError:
                # Concurrent removal is already the desired end state.
                continue
            except OSError as exc:
                raise SteamResetError(
                    f"Reset stopped while removing {name!r} from {root}: {exc}", tuple(removed)
                ) from exc
        try:
            remaining = [name for name in os.listdir(root_fd)
                         if not name.startswith(".")
                         and not any(marker in name for marker in PRESERVED_MARKERS)]
        except OSError as exc:
            raise SteamResetError(f"Could not verify the Steam reset: {exc}", tuple(removed)) from exc
        if remaining:
            raise SteamResetError("Steam files appeared during the reset. Close Steam and try again.",
                                  tuple(removed))
        return ResetResult(tuple(removed), preserved)
    finally:
        os.close(root_fd)




HELPER = "/usr/bin/steamos-add-to-steam"
LAUNCHER = "/usr/bin/bazzite-portal"


def add_to_steam():
    if not Path(HELPER).is_file():
        raise RuntimeError("Bazzite’s Add to Steam helper is unavailable.")
    if not Path(LAUNCHER).is_file():
        raise RuntimeError("Install the Bazzite Portal RPM before adding it to Steam.")
    process = subprocess.Popen([HELPER, LAUNCHER], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        status = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # The helper can keep running with Steam. Do not stop the user's client.
        return
    if status:
        raise RuntimeError(f"Add to Steam exited with status {status}.")
