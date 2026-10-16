"""SDL3 gamepad input adapter.
GTK owns windows; SDL owns gamepad input only. Default virtual filtering is retained.
"""
import ctypes
import ctypes.util
import time
import sys
try:
    from gi.repository import GLib
except ImportError:
    GLib = None

def controller_button_glyph(command, labels=None):
    """Use SDL's face-button labels, with Xbox glyphs for unknown devices."""
    labels = labels or {}
    glyphs = {1: 'Ⓐ', 2: 'Ⓑ', 3: 'Ⓧ', 4: 'Ⓨ', 5: '✕', 6: '○', 7: '▢', 8: '△'}
    defaults = {'confirm': 'Ⓐ', 'back': 'Ⓑ', 'focus_search': 'Ⓧ', 'context_action': 'Ⓨ'}
    return glyphs.get(labels.get(command), defaults[command])


def controller_legend_text(labels=None):
    select = controller_button_glyph('confirm', labels)
    back = controller_button_glyph('back', labels)
    return f'↑↓ Move · {select} Select · {back} Back'


class ControllerInput:
    """Translate SDL's positional controls into commands, without GTK dependencies."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.armed = False
        self.previous = set()
        self.direction = None
        self.repeat_at = 0

    def update(self, buttons, x, y, now):
        direction = None
        if 'up' in buttons:
            direction = 'up'
        elif 'down' in buttons:
            direction = 'down'
        elif 'left' in buttons:
            direction = 'left'
        elif 'right' in buttons:
            direction = 'right'
        elif max(abs(x), abs(y)) >= 0.35:
            if abs(y) >= abs(x):
                direction = 'down' if y > 0 else 'up'
            else:
                direction = 'right' if x > 0 else 'left'
        if not self.armed:
            self.armed = not buttons and direction is None
            return []
        commands = []
        if direction != self.direction:
            if direction:
                commands.append(direction)
                self.repeat_at = now + 0.350
        elif direction and now >= self.repeat_at:
            commands.append(direction)
            self.repeat_at = now + 0.100
        self.direction = direction
        for command in ('back', 'confirm', 'focus_search', 'context_action', 'previous_tab', 'next_tab'):
            if command in buttons and command not in self.previous:
                commands.append(command)
        self.previous = set(buttons)
        return commands


class ControllerPriority:
    """Focus grace period after controller input, independent of connection prompts."""

    def __init__(self, duration=2.0, clock=time.monotonic):
        self.duration = duration
        self.clock = clock
        self.last_activity = None
        self.native_takeover_pending = False

    def mark_activity(self):
        self.last_activity = self.clock()

    def is_recent(self):
        return (self.last_activity is not None and
                self.clock() - self.last_activity < self.duration)

    def request_native_takeover(self):
        if self.is_recent():
            self.native_takeover_pending = True
            return False
        self.native_takeover_pending = False
        return True

    def native_takeover_due(self):
        if self.native_takeover_pending and not self.is_recent():
            self.native_takeover_pending = False
            return True
        return False

    def remaining(self):
        if self.last_activity is None:
            return 0.0
        return max(0.0, self.duration - (self.clock() - self.last_activity))

    def reset(self):
        self.last_activity = None
        self.native_takeover_pending = False


class SDLGamepad:
    """Small, typed SDL3 adapter. SDL owns input only; GTK owns all windows."""

    BUTTONS = {'confirm': 0, 'back': 1, 'focus_search': 2, 'context_action': 3,
               'previous_tab': 9, 'next_tab': 10,
               'up': 11, 'down': 12, 'left': 13, 'right': 14}
    INIT_GAMEPAD = 0x00002000

    def __init__(self):
        library = ctypes.util.find_library('SDL3') or 'libSDL3.so.0'
        self.sdl = ctypes.CDLL(library)
        self.gamepads = {}
        self.connection_serial = 0
        self.button_labels = {}
        self.initialized = False
        signatures = {
            'SDL_SetHint': (ctypes.c_bool, [ctypes.c_char_p, ctypes.c_char_p]),
            'SDL_InitSubSystem': (ctypes.c_bool, [ctypes.c_uint32]),
            'SDL_QuitSubSystem': (None, [ctypes.c_uint32]),
            'SDL_GetError': (ctypes.c_char_p, []),
            'SDL_UpdateGamepads': (None, []),
            'SDL_PumpEvents': (None, []),
            'SDL_FlushEvents': (None, [ctypes.c_uint32, ctypes.c_uint32]),
            'SDL_SetGamepadEventsEnabled': (None, [ctypes.c_bool]),
            'SDL_SetJoystickEventsEnabled': (None, [ctypes.c_bool]),
            'SDL_GetGamepads': (ctypes.POINTER(ctypes.c_uint32), [ctypes.POINTER(ctypes.c_int)]),
            'SDL_free': (None, [ctypes.c_void_p]),
            'SDL_OpenGamepad': (ctypes.c_void_p, [ctypes.c_uint32]),
            'SDL_CloseGamepad': (None, [ctypes.c_void_p]),
            'SDL_GamepadConnected': (ctypes.c_bool, [ctypes.c_void_p]),
            'SDL_GetGamepadButton': (ctypes.c_bool, [ctypes.c_void_p, ctypes.c_int]),
            'SDL_GetGamepadButtonLabel': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
            'SDL_GetGamepadAxis': (ctypes.c_int16, [ctypes.c_void_p, ctypes.c_int]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.sdl, name)
            function.restype, function.argtypes = result, arguments
        # SDL has no window to track focus. The GTK dispatcher gates input instead.
        self.sdl.SDL_SetHint(b'SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', b'1')
        if not self.sdl.SDL_InitSubSystem(self.INIT_GAMEPAD):
            error = self.sdl.SDL_GetError()
            self.sdl.SDL_QuitSubSystem(self.INIT_GAMEPAD)
            raise RuntimeError(error.decode('utf-8', errors='replace') if error else 'SDL initialization failed')
        self.initialized = True
        self.sdl.SDL_SetGamepadEventsEnabled(False)
        self.sdl.SDL_SetJoystickEventsEnabled(False)

    def sample(self):
        # Linux udev hot-plug notifications are processed by the SDL event pump,
        # even when button/axis events are disabled and we read state directly.
        self.sdl.SDL_PumpEvents()
        self.sdl.SDL_UpdateGamepads()
        self.sdl.SDL_FlushEvents(0, 0xFFFF)
        for device, (handle, _serial, _labels) in list(self.gamepads.items()):
            if not self.sdl.SDL_GamepadConnected(handle):
                self.sdl.SDL_CloseGamepad(handle)
                del self.gamepads[device]
        count = ctypes.c_int()
        ids = self.sdl.SDL_GetGamepads(ctypes.byref(count))
        try:
            if ids:
                for index in range(count.value):
                    device = ids[index]
                    if device in self.gamepads:
                        continue
                    handle = self.sdl.SDL_OpenGamepad(device)
                    if handle:
                        self.connection_serial += 1
                        labels = {command: self.sdl.SDL_GetGamepadButtonLabel(handle, self.BUTTONS[command])
                                  for command in ('confirm', 'back', 'focus_search', 'context_action')}
                        self.gamepads[device] = (handle, self.connection_serial, labels)
        finally:
            if ids:
                self.sdl.SDL_free(ids)
        samples = []
        for handle, serial, labels in self.gamepads.values():
            buttons = {name for name, button in self.BUTTONS.items()
                       if self.sdl.SDL_GetGamepadButton(handle, button)}
            x = self.sdl.SDL_GetGamepadAxis(handle, 0) / 32768.0
            y = self.sdl.SDL_GetGamepadAxis(handle, 1) / 32768.0
            samples.append((serial, buttons, x, y, labels))
        return samples

    def close(self):
        for handle, _serial, _labels in self.gamepads.values():
            self.sdl.SDL_CloseGamepad(handle)
        self.gamepads.clear()
        if self.initialized:
            self.sdl.SDL_QuitSubSystem(self.INIT_GAMEPAD)
            self.initialized = False


class ControllerDriver:
    """GLib timer lifecycle and focus gating, also usable with mocked backends."""

    def __init__(self, backend, context, dispatch, scheduler=GLib, on_connections_changed=None,
                 on_activity=None):
        self.backend, self.context, self.dispatch = backend, context, dispatch
        self.on_connections_changed = on_connections_changed
        self.on_activity = on_activity
        self.scheduler = scheduler
        self.inputs = {}
        self.last_context = None
        self.connected = None
        self.source = scheduler.timeout_add(16, self.poll)

    def reset_inputs(self):
        for state in self.inputs.values():
            state.reset()

    def poll(self):
        try:
            samples = self.backend.sample()
            context = self.context()
            connected = {sample[0] for sample in samples}
            is_connected = bool(connected)
            if is_connected != self.connected:
                self.connected = is_connected
                if self.on_connections_changed:
                    self.on_connections_changed(is_connected)
            self.inputs = {serial: state for serial, state in self.inputs.items() if serial in connected}
            if context != self.last_context:
                self.reset_inputs()
                self.last_context = context
            if not context:
                self.reset_inputs()
                return True
            pending = []
            now = time.monotonic()
            active = any(buttons or max(abs(x), abs(y)) >= 0.35
                         for _serial, buttons, x, y, _labels in samples)
            if active and self.on_activity:
                self.on_activity()
            for serial, buttons, x, y, labels in samples:
                state = self.inputs.get(serial)
                if state is None:
                    state = self.inputs[serial] = ControllerInput()
                pending.extend((command, labels) for command in state.update(buttons, x, y, now))
            for command, labels in pending:
                self.backend.button_labels = labels
                self.dispatch(command)
                # Activation can open a modal or launch a terminal. Discard the
                # rest of this sample rather than dispatching into a new context.
                if command in ('confirm', 'back', 'focus_search', 'context_action') or self.context() != context:
                    self.reset_inputs()
                    break
            return True
        except Exception as error:
            print(f'Warning: Controller support disabled: {error}', file=sys.stderr)
            self.source = None
            self.backend.close()
            if self.connected and self.on_connections_changed:
                self.connected = False
                self.on_connections_changed(False)
            return False

    def close(self):
        if self.source is not None:
            self.scheduler.source_remove(self.source)
            self.source = None
        self.backend.close()
        self.inputs.clear()
        if self.connected and self.on_connections_changed:
            self.connected = False
            self.on_connections_changed(False)
