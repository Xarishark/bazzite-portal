"""Bounded, unprivileged diagnostics collection for a user-created report."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

ISSUE_URL = "https://github.com/ublue-os/bazzite/issues/new"
MAX_COMMAND_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_URL_BYTES = 7500
COMMAND_TIMEOUT = 8
JOURNAL_TIMEOUT = 60


@dataclass(frozen=True)
class Report:
    directory: Path
    hardware: str
    files: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def issue_url(self) -> str:
        return build_issue_url(self)


@dataclass(frozen=True)
class _CommandResult:
    text: str
    error: str | None = None
    truncated: bool = False
    stderr: str = ""
    stderr_truncated: bool = False


def _run_command(argv: list[str], timeout: int = COMMAND_TIMEOUT) -> _CommandResult:
    """Run a command without a shell while retaining only a bounded output buffer."""
    try:
        environment = dict(os.environ, LC_ALL="C")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    except OSError as exc:
        return _CommandResult("", f"could not start {argv[0]}: {exc}")

    chunks: list[bytes] = []
    size = 0
    overflow = False
    stderr_chunks: list[bytes] = []
    stderr_size = 0
    stderr_overflow = False

    def drain(stream, target: list[bytes], limit: int, is_stderr: bool = False) -> None:
        nonlocal size, overflow, stderr_size, stderr_overflow
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            room = (limit - stderr_size) if is_stderr else (limit - size)
            if room > 0:
                kept = chunk[:room]
                target.append(kept)
                if is_stderr:
                    stderr_size += len(kept)
                else:
                    size += len(kept)
            if len(chunk) > room:
                if is_stderr:
                    stderr_overflow = True
                else:
                    overflow = True

    assert proc.stdout is not None and proc.stderr is not None
    stdout_reader = threading.Thread(target=drain, args=(proc.stdout, chunks, MAX_COMMAND_BYTES), daemon=True)
    stderr_reader = threading.Thread(target=drain, args=(proc.stderr, stderr_chunks, 64 * 1024, True), daemon=True)
    stdout_reader.start()
    stderr_reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        stdout_reader.join()
        stderr_reader.join()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        output = b"".join(chunks).decode("utf-8", "replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
        return _CommandResult(output, f"command timed out after {timeout}s", overflow, stderr, stderr_overflow)
    stdout_reader.join()
    stderr_reader.join()
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    output = b"".join(chunks).decode("utf-8", "replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    if proc.returncode:
        return _CommandResult(output, f"command exited with status {proc.returncode}", overflow, stderr, stderr_overflow)
    return _CommandResult(output, None, overflow, stderr, stderr_overflow)


def _read(path: Path, limit: int = MAX_FILE_BYTES) -> tuple[str, str | None, bool]:
    try:
        # Open without following a final symlink. All reads remain unprivileged.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "", f"not found: {path}", False
    except OSError as exc:
        return "", f"cannot read {path}: {exc}", False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "", f"not a regular file: {path}", False
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
    finally:
        os.close(fd)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", "replace"), None, truncated


def _read_os_release(etc_path: Path = Path("/etc/os-release"),
                     usr_path: Path = Path("/usr/lib/os-release")) -> tuple[str, str | None, bool]:
    """Follow the conventional os-release symlink only to its trusted target."""
    etc_path, usr_path = Path(etc_path), Path(usr_path)
    if etc_path.is_symlink():
        try:
            target = Path(os.readlink(etc_path))
            target = target if target.is_absolute() else etc_path.parent / target
            if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(usr_path)):
                return _read(usr_path, 32 * 1024)
            return "", f"unsafe os-release symlink target: {target}", False
        except OSError as exc:
            return "", f"cannot resolve os-release symlink: {exc}", False
    return _read(etc_path, 32 * 1024)


def _write_report_file(directory: Path, name: str, text: str) -> bool:
    path = directory / name
    # O_EXCL prevents overwrites and O_NOFOLLOW prevents symlink traversal.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        payload = text.encode("utf-8", "replace")
        truncated = len(payload) > MAX_FILE_BYTES
        if truncated:
            marker = b"\n[Output truncated while saving at the file size limit]\n"
            payload = payload[:max(0, MAX_FILE_BYTES - len(marker))] + marker[-MAX_FILE_BYTES:]
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, 0o600, follow_symlinks=False)
    return truncated


def _new_report_dir(desktop: Path) -> Path:
    desktop = Path(desktop)
    # The caller supplies the desktop directory. Reject symlink roots and never
    # reuse a collision; mkdir(mode) is atomic with respect to competing runs.
    if desktop.is_symlink() or not desktop.is_dir():
        raise OSError(f"desktop directory is unavailable or unsafe: {desktop}")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    for suffix in range(1000):
        name = f"logs-{stamp}" if suffix == 0 else f"logs-{stamp}-{suffix}"
        candidate = desktop / name
        try:
            candidate.mkdir(mode=0o700)
            if os.name == "posix":
                os.chmod(candidate, 0o700, follow_symlinks=False)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique diagnostics directory")


def _cpu_info(warnings: list[str]) -> str:
    text, error, truncated = _read(Path("/proc/cpuinfo"), 1024 * 1024)
    if error:
        warnings.append(f"CPU information: {error}")
        return "CPU model: Unknown\nPhysical cores: Unknown\nLogical CPUs: Unknown\n"
    if truncated:
        warnings.append("CPU information: /proc/cpuinfo truncated")
    model = "Unknown"
    processors = 0
    pairs: set[tuple[str, str]] = set()
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                processors += 1
                if "physical id" in current and "core id" in current:
                    pairs.add((current["physical id"], current["core id"]))
                current = {}
            continue
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        current[key] = value
        if model == "Unknown" and key in {"model name", "Hardware", "Processor"}:
            model = value
    physical = str(len(pairs)) if pairs else "Unknown"
    return f"CPU model: {model}\nPhysical cores: {physical}\nLogical CPUs: {processors or 'Unknown'}\n"


def _gpu_info(warnings: list[str]) -> str:
    result = _run_command(["lspci", "-Dnn"])
    found: list[str] = []
    if result.text:
        for line in result.text.splitlines():
            if re.search(r"\b(?:VGA compatible controller|3D controller|Display controller)\b", line, re.I):
                found.append(line.strip())
    if result.error:
        warnings.append(f"GPU enumeration: {result.error}")
    if result.truncated:
        warnings.append("GPU enumeration output was truncated")
    if not found:
        devices = sorted(Path("/sys/bus/pci/devices").glob("*/class"))
        for class_file in devices:
            try:
                cls = class_file.read_text(encoding="ascii").strip().lower()
                if not cls.startswith("0x03"):
                    continue
                base = class_file.parent
                vendor = (base / "vendor").read_text(encoding="ascii").strip()
                device = (base / "device").read_text(encoding="ascii").strip()
                found.append(f"{base.name} Display controller [{vendor}:{device}]")
            except OSError:
                continue
    return "GPUs:\n" + ("\n".join(f"  {item}" for item in found) if found else "  Unknown\n")


def _hardware(warnings: list[str]) -> str:
    cpu = _cpu_info(warnings)
    ram = "Memory: Unknown\n"
    text, error, truncated = _read(Path("/proc/meminfo"), 128 * 1024)
    if error:
        warnings.append(f"Memory information: {error}")
    else:
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.M)
        if match:
            ram = f"Memory: {int(match.group(1)) // 1024} MiB ({match.group(1)} kB)\n"
        if truncated:
            warnings.append("Memory information was truncated")
    board = "Motherboard: Unknown\n"
    fields = []
    for leaf in ("board_vendor", "board_name"):
        value, err, clipped = _read(Path("/sys/class/dmi/id") / leaf, 4096)
        if err:
            warnings.append(f"Motherboard {leaf}: {err}")
        else:
            fields.append(value.strip())
        if clipped:
            warnings.append(f"Motherboard {leaf} was truncated")
    if any(fields):
        board = f"Motherboard: {' '.join(value for value in fields if value) or 'Unknown'}\n"
    return cpu + ram + board + _gpu_info(warnings)


def _collect_text(name: str, warnings: list[str], *, command: list[str] | None = None,
                  paths: tuple[Path, ...] = (), timeout: int = COMMAND_TIMEOUT) -> str:
    chunks: list[str] = []
    if command is not None:
        result = _run_command(command, timeout=timeout)
        if result.text:
            chunks.append(result.text.rstrip() + "\n")
        if result.error:
            message = f"{name}: {result.error}"
            warnings.append(message)
            chunks.append(f"\n[Collection issue: {result.error}]\n")
        if result.stderr:
            chunks.append(f"\n[Command diagnostics]\n{result.stderr.rstrip()}\n")
        if result.stderr_truncated:
            warnings.append(f"{name}: command diagnostics truncated at 65536 bytes")
            chunks.append("[Command diagnostics truncated]\n")
        if result.truncated:
            warnings.append(f"{name}: output truncated at {MAX_COMMAND_BYTES} bytes")
            chunks.append(f"\n[Output truncated at {MAX_COMMAND_BYTES} bytes]\n")
        if command and command[0] == "journalctl":
            if not result.text.strip() and not result.error:
                warning = f"{name}: no journal data was returned; the selected boot may be unavailable"
                warnings.append(warning)
                chunks.append(f"[{warning}]\n")
            access_hint = re.search(
                r"(?im)^\s*(?:hint:\s*you are currently not seeing messages from other users.*|"
                r"failed to open .*journal.*permission denied.*|permission denied: .*journal.*)$",
                result.stderr,
            )
            if access_hint:
                warning = f"{name}: journal access was denied or incomplete"
                warnings.append(warning)
                chunks.append(f"\n[Warning: {warning}]\n")
            chunks.insert(0, "# Journal entries are in chronological order.\n")
    for path in paths:
        text, error, truncated = _read(path)
        if error:
            message = f"{name}: {error}"
            warnings.append(message)
            chunks.append(f"[Could not collect {path}: {error}]\n")
            continue
        chunks.append(text)
        if text and not text.endswith("\n"):
            chunks.append("\n")
        if truncated:
            warnings.append(f"{name}: {path} truncated at {MAX_FILE_BYTES} bytes")
            chunks.append(f"[Output truncated at {MAX_FILE_BYTES} bytes]\n")
    if not chunks:
        chunks.append("No data was available.\n")
    return "".join(chunks)


def _installation(warnings: list[str]) -> str:
    os_release, error, truncated = _read_os_release()
    parts = ["Operating system:\n", os_release or "Unknown\n", "\nrpm-ostree status:\n"]
    if error:
        warnings.append(f"Installation information: {error}")
    if truncated:
        warnings.append("Installation information: /etc/os-release truncated")
    parts.append(_collect_text("rpm-ostree status", warnings, command=["rpm-ostree", "status", "--json"]))
    return "".join(parts)


def _steam_console_candidates() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".local/share/Steam/logs/console-linux.txt",
        home / ".steam/steam/logs/console-linux.txt",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/logs/console-linux.txt",
    )


def _collect_application_logs(directory: Path, warnings: list[str]) -> list[str]:
    """Copy the active application log and rotations into a report safely."""
    from bazzite_portal.core import logging as app_logging

    written: list[str] = []
    try:
        paths = app_logging.log_paths()
    except OSError as exc:
        warnings.append(f"Application logs: could not locate logs: {exc}")
        paths = []
    allowed = {app_logging.LOG_FILENAME, *(f"{app_logging.LOG_FILENAME}.{i}" for i in range(1, app_logging.BACKUP_COUNT + 1))}
    available = [path for path in paths if path.name in allowed]
    if not available:
        warnings.append("Application logs: no application log files were available")
        try:
            _write_report_file(directory, "bazzite-portal-logs.txt", "No application log files were available.\n")
            written.append("bazzite-portal-logs.txt")
        except OSError as exc:
            warnings.append(f"Could not save application log status: {exc}")
        return written

    for path in available:
        try:
            text, error, truncated = _read(path, app_logging.MAX_LOG_BYTES)
            if error:
                warnings.append(f"Application log {path.name}: {error}")
                continue
            if truncated:
                warnings.append(f"Application log {path.name}: truncated at {app_logging.MAX_LOG_BYTES} bytes")
            if _write_report_file(directory, path.name, text):
                warnings.append(f"{path.name}: output truncated while saving at {MAX_FILE_BYTES} bytes")
            written.append(path.name)
        except OSError as exc:
            warnings.append(f"Application log {path.name}: {exc}")
    return written


def _collect_report(desktop: Path, *, include_hardware_file: bool) -> Report:
    """Collect a private local bundle. No privilege escalation or upload occurs."""
    warnings: list[str] = []
    directory = _new_report_dir(Path(desktop))
    hardware = _hardware(warnings)
    written: list[str] = []
    if include_hardware_file:
        try:
            if _write_report_file(directory, "hardware.txt", hardware):
                warnings.append(f"hardware.txt: output truncated while saving at {MAX_FILE_BYTES} bytes")
            written.append("hardware.txt")
        except OSError as exc:
            warnings.append(f"Could not save hardware.txt: {exc}")
    collectors = (
        ("bazzite-installation.txt", lambda: _installation(warnings)),
        ("current-boot.txt", lambda: _collect_text("Current boot journal", warnings,
            command=["journalctl", "--no-pager", "-b", "0"], timeout=JOURNAL_TIMEOUT)),
        ("previous-boot.txt", lambda: _collect_text("Previous boot journal", warnings,
            command=["journalctl", "--no-pager", "-b", "-1"], timeout=JOURNAL_TIMEOUT)),
        ("kernel-current-boot.txt", lambda: _collect_text("Current boot kernel journal", warnings,
            command=["journalctl", "--no-pager", "-b", "0", "-k"], timeout=JOURNAL_TIMEOUT)),
    )
    for name, collect in collectors:
        try:
            if _write_report_file(directory, name, collect()):
                warnings.append(f"{name}: output truncated while saving at {MAX_FILE_BYTES} bytes")
            written.append(name)
        except OSError as exc:
            warnings.append(f"Could not save {name}: {exc}")
    written.extend(_collect_application_logs(directory, warnings))
    steam_path = next((candidate for candidate in _steam_console_candidates() if candidate.exists()), None)
    if steam_path is not None:
        try:
            if _write_report_file(directory, "steam-console.txt",
                _collect_text("Steam console log", warnings, paths=(steam_path,))):
                warnings.append(f"steam-console.txt: output truncated while saving at {MAX_FILE_BYTES} bytes")
            written.append("steam-console.txt")
        except OSError as exc:
            warnings.append(f"Could not save steam-console.txt: {exc}")
    if not written:
        try:
            directory.rmdir()
        except OSError:
            pass
        raise OSError("Could not save any diagnostics report files")
    return Report(directory, hardware, tuple(written), tuple(dict.fromkeys(warnings)))


def collect_report(desktop: Path) -> Report:
    """Collect report attachments and hardware for a reviewed GitHub issue draft."""
    return _collect_report(desktop, include_hardware_file=False)


def collect_system_logs(desktop: Path) -> Report:
    """Collect the troubleshooting log bundle plus hardware.txt locally."""
    return _collect_report(desktop, include_hardware_file=True)


def build_issue_url(report: Report) -> str:
    """Build a GitHub issue draft URL; the user still reviews and submits it."""
    hardware = _url_summary(report.hardware, 1800)
    url = ISSUE_URL + "?" + urlencode({"template": "bug-report.yml", "hardware": hardware})
    while len(url.encode("utf-8")) > MAX_URL_BYTES and hardware:
        hardware = hardware[: max(1, len(hardware) * 3 // 4)]
        url = ISSUE_URL + "?" + urlencode({"template": "bug-report.yml", "hardware": hardware})
    return url


def _url_summary(text: str, limit: int) -> str:
    # Keep embedded control characters from bloating or confusing issue fields.
    cleaned = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return cleaned[:limit].strip()
