"""Decky frontend adapter. Portal's existing agent owns all system operations."""
import asyncio
import logging
import os
from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(PLUGIN_ROOT), str(PLUGIN_ROOT / "vendor")]

import decky
from backend import messages as text
from backend.payload import compact_state
from backend.presentation import TOKEN_PATTERN, rollback_cancel_target
from backend.transport import AgentConnection, decode_snapshot
from jeepney.wrappers import DBusErrorResponse

log = logging.getLogger(__name__)
EVENT = "portal_state"
PORTAL = Path("/usr/bin/bazzite-portal")
OPERATIONS = {
    "update": "StartUpdate",
    "channel": "StartChannelSwitch",
    "rollback": "StartRollback",
    "cancel_rollback": "CancelRollback",
    "cancel_pending": "CancelPending",
    "clear_cache": "ClearDownloadCache",
}


def normal_user():
    return os.geteuid() != 0


class Plugin:
    async def _main(self):
        self.closing = False
        self.watch = None
        self.supervisor = None
        self.request = None
        self.publisher = None
        self.installer_read = None
        self.installer_request = None
        self.connect_lock = asyncio.Lock()
        self.state = {"connection": "connecting", "snapshot": None,
                      "request_pending": False, "error": "", "revision": 0,
                      "rollback_cancel_target": rollback_cancel_target(None),
                      "installation": None, "installation_error": "", "installation_pending": False}
        if not normal_user():
            self._set(connection="unavailable", error=text.NORMAL_USER)
            return
        await self._connect()

    def _set(self, **changes):
        self.state.update(changes)
        self.state["revision"] += 1
        if not self.closing and (self.publisher is None or self.publisher.done()):
            self.publisher = asyncio.create_task(self._publish())

    async def _publish(self):
        try:
            while not self.closing:
                revision = self.state["revision"]
                await decky.emit(EVENT, compact_state(self.state))
                if revision == self.state["revision"]:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A closed panel/Decky IPC transport must not affect the operation.
            log.debug("Could not deliver Portal state to Decky", exc_info=True)

    def _changed(self, kind, value):
        if self.closing:
            return
        if kind == "owner":
            self.installation_sequence = getattr(self, "installation_sequence", 0) + 1
            if not value:
                self._set(connection="unavailable", snapshot=None, error=text.UNAVAILABLE,
                          installation=None,
                          rollback_cancel_target=rollback_cancel_target(None))
            else:
                self._set(connection="connecting", error="")
        elif kind == "installation":
            self.installation_sequence = getattr(self, "installation_sequence", 0) + 1
            self._set(installation=value, installation_error="")
        elif kind == "installation_error":
            self.installation_sequence = getattr(self, "installation_sequence", 0) + 1
            self._set(installation_error=text.INSTALLER_UNAVAILABLE)
        elif kind == "snapshot":
            management = value.get("management")
            data = management.get("deployment_status") if isinstance(management, dict) else None
            recovered = self.state.get("error") in {
                text.UNAVAILABLE, text.CONNECTION_LOST, text.MALFORMED_SNAPSHOT}
            self._set(connection="connected", snapshot=value,
                      **({"error": ""} if recovered else {}),
                      rollback_cancel_target=rollback_cancel_target(data))
        else:
            log.warning("Could not read Portal snapshot: %s", value)
            self._set(connection="unavailable", error=text.MALFORMED_SNAPSHOT)





    async def _disconnect(self):
        for task in (getattr(self, "installer_read", None), getattr(self, "installer_request", None)):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._set(installation=None, installation_pending=False)
        if self.request is not None and not self.request.done():
            self.request.cancel()
            await asyncio.gather(self.request, return_exceptions=True)
            self._set(request_pending=False, error=text.REQUEST_UNCERTAIN)
        if self.watch is not None:
            await self.watch.close()
            self.watch = None

    async def _connect(self):
        async with self.connect_lock:
            await self._disconnect()
            if not normal_user():
                self._set(connection="unavailable", snapshot=None, error=text.NORMAL_USER)
                return
            if not PORTAL.is_file():
                self._set(connection="not_installed", snapshot=None, error=text.NOT_INSTALLED)
                return
            watch = AgentConnection(self._changed)
            self.watch = watch
            try:
                await asyncio.wait_for(watch.connect(), 15)
            except asyncio.CancelledError:
                await watch.close()
                raise
            except Exception:
                await watch.close()
                self.watch = None
                log.warning("Could not connect to Portal worker", exc_info=True)
                self._set(connection="unavailable", error=text.UNAVAILABLE)
                return
            if self.supervisor is None or self.supervisor.done():
                self.supervisor = asyncio.create_task(self._supervise())

    async def _supervise(self):
        while not self.closing and self.watch is not None:
            watch = self.watch
            try:
                await watch.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Portal connection ended", exc_info=True)
            if self.closing or watch is not self.watch:
                return
            self._set(connection="reconnecting", snapshot=None, error=text.CONNECTION_LOST)
            # Bounded backoff occurs only after an actual connection failure.
            # Agent absence on a healthy bus uses NameOwnerChanged, not polling.
            for delay in (0, 1, 2):
                if delay:
                    await asyncio.sleep(delay)
                await self._connect()
                if self.watch is not None:
                    break
            else:
                return

    async def get_state(self):
        return compact_state(self.state)








    async def reconnect(self):
        self._set(connection="reconnecting", snapshot=None, error="")
        if self.supervisor is not None and not self.supervisor.done():
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
        self.supervisor = None
        await self._connect()
        return compact_state(self.state)

    def _check_request(self, action, target):
        if not normal_user():
            raise RuntimeError(text.NORMAL_USER)
        if action not in OPERATIONS:
            raise ValueError(text.INVALID_ACTION)
        if self.state["connection"] != "connected" or self.watch is None:
            raise RuntimeError(text.UNAVAILABLE)
        state = self.state.get("snapshot") or {}
        management = state.get("management") or {}
        summary = management.get("summary") or {}
        transaction = summary.get("transaction")
        if transaction is not None and transaction is not False and transaction != "" and transaction != [] and transaction != {}:
            raise RuntimeError(text.BUSY)
        if self.state["request_pending"] or state.get("request_busy") or state.get("running") or any(
                state.get(kind + "_state") == "running"
                for kind in ("rollback", "cancel", "layer", "cache", "channel", "image", "delete")):
            raise RuntimeError(text.BUSY)
        if not isinstance(target, dict):
            raise ValueError(text.CHANGED)
        if action == "update" and (state.get("available") is not True or state.get("deployment")):
            raise ValueError(text.INVALID_ACTION)
        if action == "restart" and (state.get("available") is not True
                or state.get("deployment_known") is not True or not state.get("deployment")
                or target.get("deployment") != state.get("deployment")):
            raise ValueError(text.CHANGED)
        if action == "rollback":
            entries = summary.get("entries") or []
            saved = next((entry for entry in entries if isinstance(entry, dict)
                          and entry.get("role") == "Saved deployment"), None)
            prepared = any(isinstance(entry, dict) and entry.get("role") == "Next restart"
                           for entry in entries)
            if (summary.get("known") is not True or saved is None or prepared
                    or self.state["rollback_cancel_target"].get("available")
                    or not isinstance(saved.get("identity"), str)
                    or not saved["identity"].strip() or saved["identity"].strip().lower() == "unknown"
                    or target.get("identity") != saved.get("identity")
                    or target.get("role") != saved.get("role")):
                raise ValueError(text.CHANGED)
        if action == "clear_cache":
            token = target.get("token")
            if (management and management.get("known") is not True
                    or state.get("cache_available") is not True
                    or state.get("cache_state") in {"unknown", "unavailable"}
                    or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token)
                    or state.get("cache_token") != token):
                raise ValueError(text.CACHE_CHANGED)
            return "s", (token,)
        if action in {"channel", "cancel_pending", "cancel_rollback"}:
            token = target.get("token")
            if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
                raise ValueError(text.CHANGED)
            if action == "cancel_rollback":
                latest = self.state["rollback_cancel_target"]
            else:
                management = state.get("management") or {}
                latest = management.get("channel_status" if action == "channel" else "cancel_target") or {}
            if latest.get("available") is not True or latest.get("token") != token:
                raise ValueError(text.CHANGED)
            return "s", (token,)
        return None, ()

    async def request_action(self, action, target=None):
        try:
            signature, arguments = self._check_request(action, {} if target is None else target)
        except (RuntimeError, ValueError) as error:
            return {"accepted": False, "error": str(error)}
        self._set(request_pending=True, pending_action=action, error="")
        watch = self.watch
        self.request = asyncio.create_task(self._run_request(watch, OPERATIONS[action], signature, arguments))
        return {"accepted": True, "error": ""}

    async def _run_request(self, watch, method, signature, arguments):
        cancelled = False
        try:
            await watch.call(method, signature, arguments, timeout=2_147_483)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as error:
            log.warning("Portal request %s failed: %s", method, error)
            if watch is self.watch and not self.closing:
                if isinstance(error, DBusErrorResponse):
                    # Preserve Portal's own complete error text without the
                    # D-Bus exception name and tuple representation.
                    data = error.data
                    detail = data[0] if data and isinstance(data[0], str) else text.REQUEST_FAILED
                elif isinstance(error, (EOFError, ConnectionError, asyncio.TimeoutError)):
                    detail = text.REQUEST_UNCERTAIN
                else:
                    detail = str(error) or text.REQUEST_FAILED
                self._set(error=detail[:4096])
        finally:
            if watch is self.watch and not self.closing:
                self._set(request_pending=False, pending_action="")
                if not cancelled and not watch.closed:
                    # Status-only read after completion, never an operation retry.
                    await watch.snapshot()




    async def _unload(self):
        # Decky 3.3.0 closes its IPC reader before awaiting this hook. Its child
        # EOF loop can starve an awaiting hook; dispose without yielding, as
        # required for this hook to finish. Only this client closes.
        self.closing = True
        for task in (self.supervisor, self.publisher, self.request,
                     getattr(self, "installer_read", None), getattr(self, "installer_request", None)):
            if task is not None:
                task.cancel()
        if self.watch is not None:
            self.watch.close_nowait()
