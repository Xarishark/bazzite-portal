"""Gaming Mode-only, per-process Polkit authentication.

This module is imported lazily by the Gaming Mode UI. It never changes the
desktop authentication agent or stores authentication responses.
"""
import os
from pathlib import Path

from bazzite_portal import APP_ID
from bazzite_portal.maintenance.service import valid_unit

AGENT_ID = APP_ID + ".Agent"
AGENT_PATH = "/io/github/Xarishark/bazzite_portal/Agent"
OBJECT_PATH = AGENT_PATH + "/GamingAuth"
INTERFACE = AGENT_ID + ".GamingAuth"
ACTION_ID = "org.freedesktop.systemd1.manage-units"
_INTERFACE_XML = f'''<node><interface name="{INTERFACE}">
  <method name="Prepare"><arg type="t" direction="in"/><arg type="t" direction="in"/><arg type="as" direction="in"/><arg type="b" direction="out"/></method>
  <method name="Release"><arg type="t" direction="in"/></method>
</interface></node>'''
_POLKIT_AGENT_IFACE = "org.freedesktop.PolicyKit1.AuthenticationAgent"
_POLKIT_AGENT_XML = f'''<node><interface name="{_POLKIT_AGENT_IFACE}">
  <method name="BeginAuthentication"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="a{{ss}}" direction="in"/><arg type="s" direction="in"/><arg type="a(sa{{sv}})" direction="in"/></method>
  <method name="CancelAuthentication"><arg type="s" direction="in"/></method>
</interface></node>'''
_POLKIT_ROOT = "/org/freedesktop/PolicyKit1/Authority"
_AUTHORITY_IFACE = "org.freedesktop.PolicyKit1.Authority"
_CANCELLED = "org.freedesktop.PolicyKit1.Error.Cancelled"


def authorized_unit(argv):
    """Return the one fixed unit authorized by a well-formed GUI request."""
    if not isinstance(argv, (list, tuple)) or len(argv) != 4:
        return None
    executable = argv[0]
    if executable not in ("systemctl", "/usr/bin/systemctl", "/bin/systemctl"):
        return None
    if argv[1] != "start":
        return None
    if len(argv) == 4 and tuple(argv[2:]) == ("--no-block", "bazzite-portal-maintenance@rollback.service"):
        return "bazzite-portal-maintenance@rollback.service"
    if argv[2] != "--wait":
        return None
    unit = argv[3]
    if unit != "bazzite-portal-maintenance@rollback.service" and valid_unit(unit):
        return unit
    return None


def challenge_matches(action_id, details, expected_unit):
    """Limit this process listener to its exact systemd start request."""
    if (action_id != ACTION_ID or not isinstance(details, dict)
            or not isinstance(expected_unit, str) or not expected_unit):
        return False
    # Some systemd versions omit these details from BeginAuthentication.
    # The agent belongs only to the verified PID executing our fixed start
    # command. Supplied details must still agree with that command.
    return (("unit" not in details or details["unit"] == expected_unit)
            and ("verb" not in details or details["verb"] == "start"))


def _preferred_identity(identities, uid):
    identities = list(identities or ())
    for identity in identities:
        try:
            if identity.get_uid() == uid:
                return identity
        except (AttributeError, TypeError):
            continue
    return identities[0] if identities else None


def _process_start_time(pid):
    """Read Linux proc start ticks without trusting environment variables."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # comm is parenthesized and may itself contain spaces or parentheses.
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[19])  # field 22; tail begins at field 3
    except (OSError, ValueError, IndexError):
        return None


def _process_is_live(pid, start_time):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[19]) == start_time and fields[0] not in {"Z", "X"}
    except (OSError, ValueError, IndexError):
        return False


def _load_polkit():
    import gi
    gi.require_version("Polkit", "1.0")
    gi.require_version("PolkitAgent", "1.0")
    from gi.repository import Gio, GLib, Polkit, PolkitAgent
    return Gio, GLib, Polkit, PolkitAgent


class GamingAuthServer:
    """Expose tightly scoped GUI-owned authorization registration over D-Bus."""
    def __init__(self, connection, on_challenge, on_ready=None, on_error=None):
        self.connection = connection
        self.on_challenge = on_challenge
        self.on_ready = on_ready or (lambda _ready: None)
        self.on_error = on_error or (lambda _error: None)
        self._gio, self._glib, self._polkit, self._agent = _load_polkit()
        self._registrations = {}
        self._sessions = {}
        self._cookies = {}
        self._closed = False
        self._owner = None
        self._interface = self._gio.DBusNodeInfo.new_for_xml(_INTERFACE_XML).interfaces[0]
        self._object_registration = connection.register_object(
            OBJECT_PATH, self._interface, self._method_call, None, None)
        self._owner_watch = self._gio.bus_watch_name_on_connection(
            connection, AGENT_ID, self._gio.BusNameWatcherFlags.NONE,
            self._owner_appeared, self._owner_vanished)
        self._authority = self._polkit.Authority.get_sync(None)
        self._authority_owner = self._authority.get_owner()
        self._polkit_interface = self._gio.DBusNodeInfo.new_for_xml(_POLKIT_AGENT_XML).interfaces[0]

    @property
    def ready(self):
        return not self._closed and self._object_registration != 0 and self._owner is not None

    def _owner_appeared(self, _connection, _name, owner):
        if self._closed:
            return
        self._owner = owner
        self.on_ready(True)

    def _owner_vanished(self, *_args):
        self._owner = None
        self._release_all()
        if not self._closed:
            self.on_ready(False)

    def _method_call(self, _connection, sender, _path, _interface, method, parameters, invocation):
        if self._closed or not self._owner or sender != self._owner:
            invocation.return_dbus_error(INTERFACE + ".Denied", "The requesting application is unavailable.")
            return
        try:
            values = parameters.unpack()
            if method == "Prepare":
                pid, start_time, argv = values
                registered = self.prepare(pid, start_time, argv)
                invocation.return_value(self._glib.Variant("(b)", (registered,)))
            elif method == "Release":
                (pid,) = values
                self.release(pid)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error(INTERFACE + ".Invalid", "Unsupported request.")
        except Exception:
            # Do not include arguments or exception text: they can contain secrets.
            invocation.return_dbus_error(INTERFACE + ".Failed", "Gaming authorization setup failed.")
            self.on_error("Gaming-mode authorization setup failed.")

    def prepare(self, pid, start_time, argv):
        expected_unit = authorized_unit(argv)
        if (not expected_unit or type(pid) is not int or type(start_time) is not int
                or pid <= 1 or pid == os.getpid() or _process_start_time(pid) != start_time):
            return False
        try:
            process_stat = os.stat(f"/proc/{pid}")
            if process_stat.st_uid != os.getuid():
                return False
        except OSError:
            return False
        self.release(pid)
        glib = self._glib
        uid = os.getuid()
        subject = ("unix-process", {
            "pid": glib.Variant("u", pid),
            "start-time": glib.Variant("t", start_time),
            "uid": glib.Variant("u", uid),
        })
        path = OBJECT_PATH + "/Process" + str(pid)
        options = {"fallback": glib.Variant("b", True)}
        gio = self._gio
        address = gio.dbus_address_get_for_bus_sync(gio.BusType.SYSTEM, None)
        flags = (gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        system_connection = gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        object_registration = 0
        try:
            object_registration = system_connection.register_object(
                path, self._polkit_interface, self._polkit_method_call, None, None)
            if not object_registration:
                system_connection.close_sync(None)
                return False
            params = glib.Variant("((sa{sv})ssa{sv})", (subject, "C", path, options))
            system_connection.call_sync(
                "org.freedesktop.PolicyKit1", _POLKIT_ROOT, _AUTHORITY_IFACE,
                "RegisterAuthenticationAgentWithOptions", params,
                glib.VariantType.new("()"), gio.DBusCallFlags.NONE, 5000, None)
        except Exception:
            if object_registration:
                try:
                    system_connection.unregister_object(object_registration)
                except Exception:
                    pass
            try:
                system_connection.close_sync(None)
            except Exception:
                pass
            raise
        self._registrations[pid] = (subject, path, object_registration,
                                    expected_unit, system_connection, start_time)
        return True

    class _InvocationTask:
        """Adapt the agent session lifecycle to a held D-Bus method reply."""
        def __init__(self, invocation):
            self.invocation = invocation
            self.done = False

        def return_boolean(self, _value):
            self.return_value(None)

        def return_value(self, _value):
            if not self.done:
                self.done = True
                self.invocation.return_value(None)

        def return_error(self, _error):
            self.return_dbus_error(_CANCELLED, "Authentication conversation was cancelled.")

        def return_dbus_error(self, name, message):
            if not self.done:
                self.done = True
                self.invocation.return_dbus_error(name, message)

    def _polkit_method_call(self, _connection, sender, path, _interface, method, parameters, invocation):
        match = next(((pid, item) for pid, item in self._registrations.items()
                      if item[1] == path), None)
        if self._closed or match is None or sender != self._authority_owner:
            invocation.return_dbus_error(_POLKIT_AGENT_IFACE + ".Denied", "Polkit request rejected.")
            return
        pid, registration = match
        try:
            values = parameters.unpack()
            if method == "BeginAuthentication":
                action_id, message, _icon, details, cookie, raw_identities = values
                if not challenge_matches(action_id, details, registration[3]):
                    invocation.return_dbus_error(_CANCELLED, "This request is outside the allowed operation.")
                    return
                identities = self._identities(raw_identities)
                task = self._InvocationTask(invocation)
                self._begin_session(pid, task, message,
                                    identities, cookie, None)
            elif method == "CancelAuthentication":
                (cookie,) = values
                pid = self._cookies.get(cookie)
                if pid == match[0]:
                    self._cancel_session(pid)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error(_POLKIT_AGENT_IFACE + ".Invalid", "Unsupported request.")
        except Exception:
            invocation.return_dbus_error(_POLKIT_AGENT_IFACE + ".Failed", "Authentication request failed.")
            self.on_error("Gaming-mode authentication request failed.")

    def _identities(self, identities):
        available = []
        for kind, properties in identities:
            properties = {key: value.unpack() if hasattr(value, "unpack") else value
                          for key, value in properties.items()}
            if kind == "unix-user" and isinstance(properties.get("uid"), int):
                available.append(self._polkit.UnixUser.new(properties["uid"]))
            elif kind == "unix-group" and isinstance(properties.get("gid"), int):
                available.append(self._polkit.UnixGroup.new(properties["gid"]))
            elif kind == "unix-netgroup" and isinstance(properties.get("name"), str):
                available.append(self._polkit.UnixNetgroup.new(properties["name"]))
        return available

    def _return_cancelled(self, task):
        task.return_error(None)

    def _begin_session(self, pid, task, message, identities, cookie, cancellable):
        if self._closed or (cancellable is not None and cancellable.is_cancelled()):
            self._return_cancelled(task)
            return
        identity = _preferred_identity(identities, os.getuid())
        if identity is None:
            self._return_cancelled(task)
            return
        state = {"task": task, "session": None, "identity": identity,
                 "cookie": cookie, "done": False}
        self._sessions[pid] = state
        self._cookies[cookie] = pid
        session = self._agent.Session.new(state["identity"], cookie)
        state["session"] = session

        def completed(_session, _gained):
            if state["done"]:
                return
            state["done"] = True
            self._sessions.pop(pid, None)
            self._cookies.pop(cookie, None)
            self.on_challenge("completed", None, None)
            task.return_boolean(True)

        def request(_session, prompt, echo_on):
            if state["done"]:
                return
            self.on_challenge("request", prompt, bool(echo_on))

        def info(_session, text):
            if not state["done"]:
                self.on_challenge("info", text, None)

        def error(_session, text):
            if not state["done"]:
                self.on_challenge("error", text, None)

        session.connect("completed", completed)
        session.connect("request", request)
        session.connect("show-info", info)
        session.connect("show-error", error)
        if cancellable is not None:
            cancellable.connect("cancelled", lambda *_: self._cancel_session(pid))
        self.on_challenge("begin", str(message or ""), None)
        session.initiate()

    def respond(self, secret):
        if not self._sessions:
            return False
        if not isinstance(secret, str):
            return False
        state = next(iter(self._sessions.values()))
        try:
            state["session"].response(secret)
        finally:
            secret = None
        return True

    def cancel(self):
        for pid in tuple(self._sessions):
            self._cancel_session(pid)

    def _cancel_session(self, pid):
        state = self._sessions.pop(pid, None)
        if state is None or state["done"]:
            return
        state["done"] = True
        self._cookies.pop(state["cookie"], None)
        try:
            state["session"].cancel()
        except Exception:
            pass
        self.on_challenge("completed", None, None)
        self._return_cancelled(state["task"])

    def release(self, pid):
        self._cancel_session(pid)
        registration = self._registrations.get(pid)
        if registration is not None:
            try:
                # A dead UnixProcess cannot be resolved by polkit during
                # unregister. Closing this dedicated connection also removes
                # the daemon-side agent registration by its unique bus name.
                if _process_is_live(pid, registration[5]):
                    params = self._glib.Variant(
                        "((sa{sv})s)", (registration[0], registration[1]))
                    registration[4].call_sync(
                        "org.freedesktop.PolicyKit1", _POLKIT_ROOT, _AUTHORITY_IFACE,
                        "UnregisterAuthenticationAgent", params,
                        self._glib.VariantType.new("()"), self._gio.DBusCallFlags.NONE,
                        3000, None)
            finally:
                try:
                    registration[4].unregister_object(registration[2])
                except Exception:
                    pass
                try:
                    registration[4].close_sync(None)
                except Exception:
                    pass
                self._registrations.pop(pid, None)

    def _release_all(self):
        for pid in tuple(self._registrations):
            try:
                self.release(pid)
            except Exception:
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._release_all()
        if self._owner_watch:
            self._gio.bus_unwatch_name(self._owner_watch)
            self._owner_watch = 0
        if self._object_registration:
            self.connection.unregister_object(self._object_registration)
            self._object_registration = 0
        self.on_ready(False)
