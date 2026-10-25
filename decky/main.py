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


    async def get_state(self):
        return compact_state(self.state)















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
