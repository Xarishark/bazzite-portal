"""Bound journal input separately from the small retained update log."""
import json


MAX_RECORD_BYTES = 1024 * 1024
MAX_TEXT = 2048


def _bounded(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + " [truncated]"
    if value is None or type(value) in (bool, int, float):
        return value
    if depth >= 3:
        return "[nested data omitted]"
    if isinstance(value, dict):
        return {str(key)[:128]: (_bounded(item[-MAX_TEXT:], depth + 1)
                                if key == "Stdout" and isinstance(item, str) else _bounded(item, depth + 1))
                for key, item in list(value.items())[:32]}
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:32]]
    return "[unsupported data omitted]"


def compact_record(raw):
    """Keep event fields when UUPD embeds a large skopeo result/error output.

    Return None for an unparseable large record, so the reader can explicitly
    mark incomplete evidence rather than silently declare success.
    """
    text = raw.decode("utf-8", errors="replace").rstrip()
    if len(raw) <= 8192:
        return text
    try:
        envelope = json.loads(text)
        message = envelope.get("MESSAGE") if isinstance(envelope, dict) else None
        data = json.loads(message) if isinstance(message, str) else envelope
        if not isinstance(data, dict):
            return None
        # The inspection payload is diagnostic only; the boolean is the event.
        if data.get("msg") == "Executed update check":
            data.pop("output", None)
        bounded = _bounded(data)
        message_text = data.get("msg")
        if (isinstance(message_text, str) and message_text.startswith("BAZZITE_PORTAL_BREW_PLAN ")
                and len(message_text.encode("utf-8")) <= 256 * 1024):
            # The adapter's bounded package inventory is itself an event, not
            # disposable diagnostic text. Its item/schema limits are validated
            # by the model before any total is used.
            bounded["msg"] = message_text
        compact = json.dumps(bounded, ensure_ascii=False)
        if isinstance(message, str):
            compact = json.dumps({"MESSAGE": compact,
                                  "__REALTIME_TIMESTAMP": envelope.get("__REALTIME_TIMESTAMP")},
                                 ensure_ascii=False)
        return compact
    except (ValueError, TypeError, RecursionError):
        return None


def records(stream):
    """Read/reassemble at most 1 MiB; journald splits long stdout lines.

    Only fragments of the same stream/PID may join. One pending message keeps
    transient storage bounded even if unrelated records arrive between chunks.
    """
    pending = None
    while True:
        raw = stream.readline(MAX_RECORD_BYTES + 1)
        if not raw:
            if pending is not None:
                yield None
            return
        if len(raw) > MAX_RECORD_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(8193)
            yield None
            pending = None
            continue
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            yield compact_record(raw)
            continue
        if not isinstance(envelope, dict):
            yield compact_record(raw)
            continue
        message = envelope.get("MESSAGE")
        stream_id, pid = envelope.get("_STREAM_ID"), envelope.get("_PID")
        identity = (stream_id, pid) if isinstance(stream_id, str) and stream_id and isinstance(pid, str) else None
        fragment = envelope.get("_LINE_BREAK") == "line-max"
        if pending is not None and identity == pending[0]:
            if not isinstance(message, str) or pending[3] + len(message.encode("utf-8")) > MAX_RECORD_BYTES:
                yield None
                pending = None
                continue
            pending[1].append(message)
            pending[3] += len(message.encode("utf-8"))
            if fragment:
                continue
            envelope = {"MESSAGE": "".join(pending[1]), "__REALTIME_TIMESTAMP": pending[2]}
            pending = None
            raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        elif fragment:
            if pending is not None:
                yield None
            pending = None
            if identity is None or not isinstance(message, str):
                yield None
            else:
                pending = [identity, [message], envelope.get("__REALTIME_TIMESTAMP"), len(message.encode("utf-8"))]
            continue
        yield compact_record(raw)
