"""Steam reset and native shortcut/artwork integration, independent of GTK."""

from __future__ import annotations

import os
import json
import logging
import re
import shlex
import tempfile
import threading
import uuid
from contextlib import contextmanager
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
DESKTOP = "/usr/share/applications/io.github.Xarishark.bazzite_portal.desktop"
STEAM_ARTWORK = Path(__file__).resolve().parents[1] / "assets" / "steam"
_ARTWORK = {"p": "cover.png", "": "landscape.png", "_hero": "hero.png", "_logo": "logo.png"}
_ARTWORK_MUTEX = threading.Lock()
log = logging.getLogger(__name__)


def pending_artwork_path():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "bazzite-portal" / "steam-artwork.json"


@contextmanager
def _artwork_locked():
    base = pending_artwork_path().parent
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _ARTWORK_MUTEX:
        fd = os.open(base / ".steam-artwork.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _bounded_read(path, limit=16 * 1024 * 1024):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Steam configuration exceeds the supported size")
    return data


def _binary_vdf(data):
    """Read Steam's binary KeyValues without modifying shortcuts.vdf."""
    position = 0

    def string():
        nonlocal position
        end = data.index(b"\0", position)
        value = data[position:end].decode("utf-8", errors="replace")
        position = end + 1
        return value

    def object_value(depth=0):
        nonlocal position
        if depth > 24:
            raise ValueError("Steam shortcut data is nested too deeply")
        result = {}
        while position < len(data):
            kind = data[position]
            position += 1
            if kind == 8:
                return result
            key = string()
            if kind == 0:
                value = object_value(depth + 1)
            elif kind == 1:
                value = string()
            elif kind in (2, 3, 4, 6, 7, 10):
                size = 8 if kind in (7, 10) else 4
                if position + size > len(data):
                    raise ValueError("Truncated Steam shortcut value")
                value = int.from_bytes(data[position:position + size], "little")
                position += size
            else:
                raise ValueError("Unsupported Steam shortcut value type")
            result[key.lower()] = value
        raise ValueError("Truncated Steam shortcut object")

    result = object_value()
    if position != len(data):
        raise ValueError("Trailing Steam shortcut data")
    return result


def _text_vdf(data):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[{}]', data.decode("utf-8-sig"))
    position = 0

    def object_value(nested=False, depth=0):
        nonlocal position
        if depth > 24:
            raise ValueError("Steam login data is nested too deeply")
        result = {}
        while position < len(tokens):
            key = tokens[position]
            position += 1
            if key == "}" and nested:
                return result
            if not key.startswith('"') or position == len(tokens):
                raise ValueError("Malformed Steam login data")
            value = tokens[position]
            position += 1
            key = json.loads(key).lower()
            result[key] = object_value(True, depth + 1) if value == "{" else json.loads(value)
        if nested:
            raise ValueError("Truncated Steam login data")
        return result

    return object_value()


def _steam_root():
    home = Path.home()
    candidates = (home / ".local/share/Steam", home / ".steam/steam",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam")
    return next((path.resolve() for path in candidates if path.is_dir()), candidates[0])


def _steam_account(root):
    try:
        users = _text_vdf(_bounded_read(root / "config/loginusers.vdf", 1024 * 1024)).get("users", {})
        recent = [str(int(key) & 0xffffffff) for key, value in users.items()
                  if key.isdecimal() and isinstance(value, dict) and value.get("mostrecent") == "1"]
        if len(recent) == 1:
            return recent[0]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    userdata = root / "userdata"
    profiles = [path.name for path in userdata.iterdir() if path.name.isdecimal() and path.is_dir()] if userdata.is_dir() else []
    return profiles[0] if len(profiles) == 1 else None


def _portal_shortcuts(root, account):
    if account is None:
        return ()
    path = root / "userdata" / account / "config/shortcuts.vdf"
    try:
        shortcuts = _binary_vdf(_bounded_read(path)).get("shortcuts", {})
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as error:
        log.warning("Could not read Steam shortcuts for artwork: %s", error)
        return ()
    if not isinstance(shortcuts, dict):
        return ()
    matches = []
    for entry in shortcuts.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("exe"), str):
            continue
        try:
            command = shlex.split(entry["exe"])
        except ValueError:
            continue
        appid = entry.get("appid")
        if (command and command[0] in (LAUNCHER, "bazzite-portal", DESKTOP)
                and isinstance(appid, int) and 0 < appid <= 0xffffffff):
            matches.append(appid)
    return tuple(sorted(set(matches)))


def _pending_artwork():
    try:
        request = json.loads(_bounded_read(pending_artwork_path(), 4096))
        account = request.get("account")
        if (request.get("version") == 1 and isinstance(request.get("id"), str)
                and isinstance(request.get("created"), (int, float))
                and (account is None or isinstance(account, str) and account.isdecimal() and len(account) <= 10)):
            return request
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_artwork_request(request):
    path = pending_artwork_path()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".steam-artwork-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_steam_artwork(root, account, appids):
    config = root / "userdata" / account / "config"
    grid = config / "grid"
    if any(path.is_symlink() for path in (root / "userdata", config.parent, config, grid)):
        raise RuntimeError("Steam artwork directory must not be a symbolic link")
    grid.mkdir(exist_ok=True)
    for appid in appids:
        for suffix, source in _ARTWORK.items():
            stem = str(appid) + suffix
            if any((grid / (stem + ext)).exists() or (grid / (stem + ext)).is_symlink()
                   for ext in (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")):
                continue
            data = (STEAM_ARTWORK / source).read_bytes()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=grid, prefix=".bazzite-portal-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if hasattr(os, "fchmod"):
                        os.fchmod(stream.fileno(), 0o644)
                # Publishing by link never overwrites a concurrently added custom image.
                try:
                    os.link(temporary, grid / (stem + ".png"))
                except FileExistsError:
                    pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def _apply_artwork(request):
    root = _steam_root()
    account = request.get("account") or _steam_account(root)
    if account is None:
        return True
    if request.get("account") is None:
        request["account"] = account
        _save_artwork_request(request)
    appids = _portal_shortcuts(root, account)
    if not appids:
        return True
    _copy_steam_artwork(root, account, appids)
    pending_artwork_path().unlink(missing_ok=True)
    log.info("Steam shortcut artwork applied; existing custom artwork preserved")
    return False


def apply_pending_artwork():
    """Return whether artwork is still pending; never alter Steam shortcuts."""
    if not pending_artwork_path().is_file():
        return False
    with _artwork_locked():
        request = _pending_artwork()
        return _apply_artwork(request) if request else False


def artwork_watch_paths():
    if not pending_artwork_path().is_file():
        return ()
    root = _steam_root()
    request = _pending_artwork() or {}
    account = request.get("account") or _steam_account(root)
    paths = [root, root / "config", root / "userdata"]
    if account:
        paths.append(root / "userdata" / account / "config")
    existing = set()
    for path in paths:
        while not path.is_dir() and path != path.parent:
            path = path.parent
        existing.add(path)
    return tuple(sorted(existing))


def add_to_steam():
    """Request a native Steam shortcut; return True when artwork is ready."""
    if not Path(HELPER).is_file():
        raise RuntimeError("Bazzite’s Add to Steam helper is unavailable.")
    if not Path(LAUNCHER).is_file() or not Path(DESKTOP).is_file():
        raise RuntimeError("Install the Bazzite Portal RPM before adding it to Steam.")
    if not all((STEAM_ARTWORK / name).is_file() for name in _ARTWORK.values()):
        raise RuntimeError("Bundled Steam artwork is unavailable")
    with _artwork_locked():
        request = _pending_artwork()
        root = _steam_root()
        account = _steam_account(root)
        if request and time.time() - request["created"] < 15:
            return not _apply_artwork(request)
        request = {"version": 1, "id": uuid.uuid4().hex, "account": account, "created": time.time()}
        _save_artwork_request(request)
        if _portal_shortcuts(root, account):
            return not _apply_artwork(request)
        try:
            process = subprocess.Popen([HELPER, DESKTOP], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                status = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Steam owns the request. Never stop its client or helper.
                status = 0
            if status:
                raise RuntimeError(f"Add to Steam exited with status {status}.")
        except Exception:
            pending_artwork_path().unlink(missing_ok=True)
            raise
    # A short worker-only wait handles normal immediate client saves. The session
    # agent watches for a later save instead of polling throughout the session.
    for _attempt in range(8):
        if not apply_pending_artwork():
            return True
        time.sleep(0.25)
    return False
