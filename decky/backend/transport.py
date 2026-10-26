"""Event-driven Jeepney connection to Portal's existing user-session agent."""
import asyncio
import json
import os
from pathlib import Path
import stat

from jeepney import DBusAddress, HeaderFields, MessageFlag, MessageType, new_method_call
from jeepney.bus_messages import MatchRule, message_bus
from jeepney.io.asyncio import open_dbus_router
from jeepney.wrappers import unwrap_msg

from . import messages as text

AGENT = "io.github.Xarishark.bazzite_portal.Agent"
AGENT_PATH = "/io/github/Xarishark/bazzite_portal/Agent"
SNAPSHOT_LIMIT = 2 * 1024 * 1024


def session_address():
    # Gaming Mode Decky does not inherit DBUS_SESSION_BUS_ADDRESS. Use only the
    # current UID's normal session socket; never guess another user's bus.
    uid = os.geteuid()
    if uid == 0:
        raise RuntimeError(text.NORMAL_USER)
    socket = Path(f"/run/user/{uid}/bus")
    metadata = socket.stat()
    if metadata.st_uid != uid or not stat.S_ISSOCK(metadata.st_mode):
        raise RuntimeError(text.BUS_UNAVAILABLE)
    return "unix:path=" + str(socket)


def decode_snapshot(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > SNAPSHOT_LIMIT:
        raise ValueError(text.MALFORMED_SNAPSHOT)
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError(text.MALFORMED_SNAPSHOT)
    return result


class AgentConnection:
    def __init__(self, changed):
        self.changed = changed
        self.owner = ""
        self.sequence = 0
        self.owner_sequence = 0
        self.tasks = set()
        self.closed = False
        self.context = None
        self.filter = None
        self.receiver = None
        self.address = DBusAddress(AGENT_PATH, bus_name=AGENT, interface=AGENT)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def call(self, method, signature=None, arguments=(), *, timeout=15):
        if self.closed:
            raise ConnectionError(text.CONNECTION_LOST)
        message = new_method_call(self.address, method, signature, arguments)
        message.header.flags |= MessageFlag.no_auto_start
        reply = await asyncio.wait_for(self.router.send_and_get_reply(message), timeout)
        return unwrap_msg(reply)

    async def snapshot(self):
        owner, sequence = self.owner, self.sequence
        try:
            reply = await self.call("GetSnapshot")
            data = decode_snapshot(reply[0])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.closed and owner == self.owner:
                self.changed("error", error)
            return
        # Subscribe before reading; a newer Changed signal always wins over an
        # older initial GetSnapshot reply or a reply from a replaced owner.
        if not self.closed and owner == self.owner and sequence == self.sequence:
            self.changed("snapshot", data)

    async def receive(self):
        while not self.closed:
            message = await self.filter.queue.get()
            fields = message.header.fields
            if (fields.get(HeaderFields.sender) == "org.freedesktop.DBus"
                    and fields.get(HeaderFields.interface) == "org.freedesktop.DBus"
                    and fields.get(HeaderFields.member) == "NameOwnerChanged"
                    and len(message.body) == 3 and message.body[0] == AGENT):
                self.owner = message.body[2]
                self.owner_sequence += 1
                self.sequence += 1
                self.changed("owner", bool(self.owner))
                if self.owner:
                    self.task(self.snapshot())
            elif (self.owner and fields.get(HeaderFields.sender) == self.owner
                    and fields.get(HeaderFields.path) == AGENT_PATH
                    and fields.get(HeaderFields.interface) == AGENT
                    and fields.get(HeaderFields.member) in {"Changed", "DeckyChanged"}):
                installation = fields.get(HeaderFields.member) == "DeckyChanged"
                if not installation:
                    self.sequence += 1
                try:
                    self.changed("installation" if installation else "snapshot", decode_snapshot(message.body[0]))
                except (ValueError, TypeError, IndexError, RecursionError) as error:
                    self.changed("installation_error" if installation else "error", error)

    async def connect(self):
        self.context = open_dbus_router(bus=session_address())
        self.router = await self.context.__aenter__()
        self.filter = self.router.filter(MatchRule(type="signal"), bufsize=64)
        self.receiver = self.task(self.receive())
        rules = [
            f"type='signal',sender='{AGENT}',interface='{AGENT}',member='Changed',path='{AGENT_PATH}'",
            f"type='signal',sender='{AGENT}',interface='{AGENT}',member='DeckyChanged',path='{AGENT_PATH}'",
            f"type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{AGENT}'",
        ]
        for rule in rules:
            unwrap_msg(await self.router.send_and_get_reply(message_bus.AddMatch(rule)))
        sequence = self.owner_sequence
        reply = await self.router.send_and_get_reply(message_bus.GetNameOwner(AGENT))
        if sequence != self.owner_sequence:
            return
        if reply.header.message_type == MessageType.error:
            self.owner = ""
            self.changed("owner", False)
        else:
            self.owner = reply.body[0]
            self.changed("owner", True)
            await self.snapshot()

    async def wait_closed(self):
        waiter = asyncio.create_task(self.context.conn.writer.wait_closed())
        try:
            await asyncio.wait((waiter, self.receiver), return_when=asyncio.FIRST_COMPLETED)
            if self.receiver.done() and not self.receiver.cancelled():
                error = self.receiver.exception()
                if error:
                    raise error
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def close_nowait(self):
        if self.closed:
            return
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        if self.filter is not None:
            self.filter.close()
            self.filter = None
        if self.context is not None and self.context.conn is not None:
            self.context.conn.writer.close()

    async def close(self):
        self.close_nowait()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        context, self.context = self.context, None
        if context is not None and context.req_ctx is not None:
            try:
                await asyncio.wait_for(context.__aexit__(None, None, None), 3)
            except (EOFError, ConnectionError, asyncio.TimeoutError):
                pass
