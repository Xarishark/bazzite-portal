"""Decky wire data: compact status and on-demand incremental logs."""
from secrets import token_hex

LOG_KEYS = ("logs", "rollback_logs", "cancel_logs", "channel_logs", "cache_logs")
MODULE_FIELDS = {"id", "name", "status", "detail", "error", "fraction", "source"}
MANAGEMENT_FIELDS = {"known", "error", "summary", "cancel_target", "channel_status"}


def compact_state(state):
    return dict(state)


def _overlap(previous, current):
    """Longest old suffix matching the new prefix, including journal rotation."""
    # Prefix-function matching is linear even for thousands of repeated lines.
    values = current + [None] + previous
    lengths = [0] * len(values)
    for index in range(1, len(values)):
        matched = lengths[index - 1]
        while matched and values[index] != values[matched]:
            matched = lengths[matched - 1]
        if values[index] == values[matched]:
            matched += 1
        lengths[index] = matched
    return lengths[-1] if lengths else 0


class LogReader:
    """One bounded previous view; stale/reopened clients receive a fresh reset."""
    def __init__(self):
        self.cursor = None
        self.sections = {}

    def read(self, snapshot, cursor):
        snapshot = snapshot or {}
        patches, sections = {}, {}
        reset = True
        for key in LOG_KEYS:
            kind = key.removesuffix("_logs")
            identity = ((snapshot.get("service"), snapshot.get("run")) if key == "logs" else
                        (snapshot.get(kind + "_unit"), snapshot.get(kind + "_invocation")))
            lines = snapshot.get(key)
            current = [line for line in lines if isinstance(line, str)] if isinstance(lines, list) else []
            old_identity, previous = self.sections.get(key, (None, []))
            sections[key] = (identity, current)
            if reset or old_identity != identity:
                patches[key] = {"reset": True, "remove": 0, "lines": current}
            elif previous != current:
                overlap = _overlap(previous, current)
                patches[key] = {"reset": False, "remove": len(previous) - overlap,
                                "lines": current[overlap:]}
        self.sections = sections
        self.cursor = token_hex(16)
        return {"cursor": self.cursor, "sections": patches}
