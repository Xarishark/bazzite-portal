"""Explicit bundled-plugin installation through Decky Loader's own prompt.

Decky v3.3.0's /ws router permits only one frontend connection, so opening a
second connection would disconnect Steam. Use its authenticated HTTP utility
bridge to request installation through the already-connected Decky frontend.
The Loader reads the fixed local ZIP itself, owns confirmation/replacement, and
keeps plugin data/settings outside the replaceable plugin directory.

API/source references (also checked against Loader main):
SteamDeckHomebrew/decky-loader v3.3.0 backend/decky_loader/{utilities,browser,
wsrouter,injector}.py and frontend/src/wsrouter.ts. No caller-supplied URL, file
path, JavaScript or operation name crosses this module's public entry points.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any
import zipfile

from bazzite_portal.core.i18n import N_

BUNDLE = Path("/usr/share/bazzite-portal/decky/bazzite-portal.zip")
PLUGIN_NAME = "Bazzite Portal"
PLUGIN_DIRECTORY = "bazzite-portal"
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 32 * 1024 * 1024
MAX_FILES = 512
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
SHARED_CONTEXTS = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")

NORMAL_USER = N_("Plugin installation must run as your normal desktop user.")
MISSING_BUNDLE = N_("This Portal installation does not include the Decky plugin.")
INVALID_BUNDLE = N_("The bundled Decky plugin could not be verified. Reinstall Bazzite Portal and try again.")
NO_DECKY = N_("Decky Loader is not installed.")
NOT_RUNNING = N_("Decky Loader is not running. Start it and try again.")
NO_FRONTEND = N_("Open Steam in Gaming Mode or Big Picture, then try again.")
UNKNOWN_PLUGIN = N_("The installed Decky plugin version could not be verified.")
CURRENT = N_("The installed Decky plugin is up to date.")
CHANGED = N_("The bundled plugin or installed version changed. Review it again.")
PROMPT = N_("Confirm the installation in Decky Loader. No change is made if you cancel.")
REQUEST_FAILED = N_("Could not request plugin installation. Open Steam in Gaming Mode or Big Picture and try again.")


def _version(value: Any) -> tuple[int, int, int] | None:
    return tuple(map(int, value.split("."))) if isinstance(value, str) and VERSION.fullmatch(value) else None


def _json(data: bytes, limit=64 * 1024) -> Any:
    if len(data) > limit:
        raise ValueError("Oversized JSON")
    return json.loads(data.decode("utf-8"))


def _bundle(path: Path | None = None) -> dict[str, str]:
    """Validate the fixed package without extracting anything into user storage."""
    path = BUNDLE if path is None else path
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_ZIP_BYTES:
        raise ValueError("Invalid plugin archive")
    raw = path.read_bytes()
    if len(raw) != metadata.st_size:
        raise ValueError("Plugin archive changed while reading")
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if not entries or len(entries) > MAX_FILES or len(names) != len(set(names)):
            raise ValueError("Invalid archive inventory")
        if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
            raise ValueError("Oversized plugin archive")
        for entry in entries:
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or ".." in name.parts or "\\" in entry.filename
                    or len(name.parts) < 2 or name.parts[0] != PLUGIN_DIRECTORY
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Invalid archive path")
        prefix = PLUGIN_DIRECTORY + "/"
        package = _json(archive.read(prefix + "package.json"))
        plugin = _json(archive.read(prefix + "plugin.json"))
        info = _json(archive.read(prefix + "build-info.json"))
        if (not isinstance(package, dict) or package.get("name") != PLUGIN_DIRECTORY
                or _version(package.get("version")) is None
                or package.get("type") != "module"
                or set(package) - {"name", "version", "type", "description", "license"}
                or not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME
                or plugin.get("flags") != [] or plugin.get("api_version") != 1
                or not isinstance(info, dict) or info.get("version") != package["version"]
                or not isinstance(info.get("revision"), str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", info["revision"])
                or not isinstance(info.get("files"), dict)):
            raise ValueError("Invalid plugin identity")
        hashes = info["files"]
        payload_names = {name[len(prefix):] for name in names if not name.endswith("/")}
        if set(hashes) != payload_names - {"build-info.json"}:
            raise ValueError("Incomplete plugin manifest")
        for name, digest in hashes.items():
            if not isinstance(digest, str) or not TOKEN.fullmatch(digest):
                raise ValueError("Invalid manifest digest")
            if hashlib.sha256(archive.read(prefix + name)).hexdigest() != digest:
                raise ValueError("Plugin payload digest mismatch")
        if "main.py" not in hashes or "dist/index.js" not in hashes:
            raise ValueError("Incomplete plugin package")
        return {"version": package["version"], "token": hashlib.sha256(raw).hexdigest(),
                "revision": info["revision"]}


def _http(port: int, method: str, path: str, body: dict | None = None,
          token: str = "", timeout: float = 3) -> bytes:
    """Fixed loopback endpoint; no proxy, redirect, shell or credential logging."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["X-Decky-Auth"] = token
    try:
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise OSError("Decky request unavailable")
        result = response.read(1024 * 1024 + 1)
        if len(result) > 1024 * 1024:
            raise ValueError("Oversized Decky reply")
        return result
    finally:
        connection.close()


class Loader:
    """Only the Loader's established HTTP bridge, never its single-client /ws."""

    def __init__(self):
        token = _http(1337, "GET", "/auth/token").decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9-]{16,128}", token):
            raise ValueError("Invalid Decky authentication response")
        self._token = token
        reply = self._method("ping", {})
        if reply != "pong":
            raise ValueError("Invalid Decky ping response")

    def _method(self, name: str, arguments: dict, timeout=3):
        reply = _json(_http(1337, "POST", "/methods/" + name, arguments, self._token, timeout), 1024 * 1024)
        if not isinstance(reply, dict) or reply.get("success") is not True:
            raise ValueError("Decky utility failed")
        return reply.get("result")

    def frontend(self) -> str:
        # Same endpoint and tab predicate as Loader's injector.py. Read tabs first
        # so an absent Steam client cannot leave Loader injection tasks waiting.
        tabs = _json(_http(8080, "GET", "/json"), 1024 * 1024)
        if not isinstance(tabs, list):
            raise ValueError("Invalid Steam frontend list")
        for tab in tabs:
            if (isinstance(tab, dict) and tab.get("title") in SHARED_CONTEXTS
                    and isinstance(tab.get("url"), str)
                    and ("https://steamloopback.host/routes/" in tab["url"]
                         or "https://steamloopback.host/index.html" in tab["url"])):
                return tab["title"]
        raise ValueError("Decky Steam frontend is unavailable")

    def execute(self, tab: str, code: str):
        reply = self._method("execute_in_tab", {"tab": tab, "run_async": True, "code": code}, timeout=8)
        if not isinstance(reply, dict) or reply.get("success") is not True:
            raise ValueError("Decky frontend unavailable")
        result = reply.get("result")
        if not isinstance(result, str):
            raise ValueError("Invalid Decky frontend response")
        return _json(result.encode(), 1024 * 1024)

    def plugins(self, tab: str):
        return self.execute(tab, "(async()=>{if(!window.DeckyBackend)throw Error('Decky unavailable');"
            "return JSON.stringify(await window.DeckyBackend.call('loader/get_plugins'));})()")

    def request_install(self, tab: str, archive: Path, version: str, digest: str, updating: bool):
        arguments = [archive.as_uri(), PLUGIN_NAME, version, digest, 2 if updating else 0]
        encoded = json.dumps(arguments, ensure_ascii=True)
        result = self.execute(tab, "(async()=>{if(!window.DeckyBackend)throw Error('Decky unavailable');"
            "await window.DeckyBackend.call('utilities/install_plugin',..." + encoded + ");"
            "return JSON.stringify({prompt_requested:true});})()")
        if not isinstance(result, dict) or result.get("prompt_requested") is not True:
            raise ValueError("Decky did not accept the installation prompt")


def _decky_present() -> bool:
    return any(path.exists() for path in (
        Path.home() / "homebrew/services/PluginLoader",
        Path.home() / "homebrew/plugins",
        Path("/etc/systemd/system/plugin_loader.service"),
        Path("/usr/lib/systemd/system/plugin_loader.service"),
    ))


def _cached_bundle(token: str) -> Path:
    """Keep confirmed bytes available while Loader's prompt waits for the user.

    Loader validates its hash after removing the prior plugin. A content-addressed
    copy avoids that replacement hazard if the RPM changes while a prompt is open.
    This cache contains only our verified ZIP, never extracted plugin/settings.
    """
    data = BUNDLE.read_bytes()
    if not 0 < len(data) <= MAX_ZIP_BYTES or hashlib.sha256(data).hexdigest() != token:
        raise ValueError("Plugin archive changed before installation")
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "bazzite-portal/decky"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    target = root / (token + ".zip")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".bundle-", dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, target)
        return target.resolve()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def status() -> dict[str, Any]:
    """One bounded, on-demand check. No timer, prompt or installation is started."""
    result = {"decky_available": False, "decky_running": False, "loader_available": False, "frontend_available": False,
              "installed_known": False, "plugin_installed": None, "installed_version": "",
              "bundled_version": "", "bundle_token": "", "source_revision": "",
              "can_install": False, "can_update": False, "can_downgrade": False, "reason": "", "error": ""}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return {**result, "reason": NORMAL_USER, "error": NORMAL_USER}
    bundle_error = ""
    try:
        bundle = _bundle()
        result.update(bundled_version=bundle["version"], bundle_token=bundle["token"], source_revision=bundle["revision"])
    except FileNotFoundError:
        bundle_error = MISSING_BUNDLE
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError, zipfile.BadZipFile, UnicodeError):
        bundle_error = INVALID_BUNDLE
    result["decky_available"] = _decky_present()
    try:
        loader = Loader()
        result.update(decky_available=True, decky_running=True, loader_available=True)
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        return {**result, "reason": NOT_RUNNING if result["decky_available"] else NO_DECKY}
    try:
        tab = loader.frontend()
        plugins = loader.plugins(tab)
        if not isinstance(plugins, list) or len(plugins) > 256:
            raise ValueError("Invalid plugin inventory")
        installed = [plugin for plugin in plugins if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME]
        if len(installed) > 1:
            raise ValueError("Ambiguous Portal plugin")
        result.update(frontend_available=True, installed_known=True, plugin_installed=bool(installed))
        if installed:
            version = installed[0].get("version")
            if _version(version) is None:
                return {**result, "reason": UNKNOWN_PLUGIN, "error": UNKNOWN_PLUGIN}
            result["installed_version"] = version
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, http.client.HTTPException):
        return {**result, "reason": NO_FRONTEND}
    if bundle_error:
        return {**result, "reason": bundle_error, "error": bundle_error}
    if not result["plugin_installed"]:
        result["can_install"] = True
    elif _version(result["installed_version"]) < _version(result["bundled_version"]):
        result["can_update"] = True
    elif _version(result["installed_version"]) > _version(result["bundled_version"]):
        result["can_downgrade"] = True
    else:
        result["reason"] = CURRENT
    return result


def install(expected_bundle_token: str) -> dict[str, Any]:
    """Request Loader's own confirmation for this exact fixed bundled ZIP.

    The reply means a confirmation was requested, never that installation has
    completed. Cancel leaves the plugin alone. A later inspect confirms version.
    """
    current = status()
    current["prompt_requested"] = False
    if (not isinstance(expected_bundle_token, str) or not TOKEN.fullmatch(expected_bundle_token)
            or current["bundle_token"] != expected_bundle_token):
        return {**current, "error": CHANGED}
    if not (current["can_install"] or current["can_update"] or current["can_downgrade"]):
        return {**current, "error": current["reason"] or REQUEST_FAILED}
    try:
        loader = Loader()
        archive = _cached_bundle(expected_bundle_token)
        loader.request_install(loader.frontend(), archive, current["bundled_version"], expected_bundle_token,
                               current["can_update"] or current["can_downgrade"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, http.client.HTTPException):
        return {**current, "error": REQUEST_FAILED}
    return {**current, "prompt_requested": True, "reason": PROMPT, "error": ""}
