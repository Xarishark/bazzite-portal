import { useEffect, useState } from "react";
import { callable } from "@decky/api";
import { LogKey, LogReply, LogSections } from "./types";
import { currentState, subscribeState } from "./state";
import { messages as m } from "./messages";

const getLogs = callable<[string | null], LogReply>("get_logs");
interface LogView {sections: LogSections; loading: boolean; error: string}

/** Subscribe only while Logs is mounted; coalesce changes behind one IPC read. */
export function watchLogs(changed: (view: LogView) => void) {
  let closed = false, pending = false, dirty = false, epoch = 0;
  let cursor: string | null = null;
  let sections: LogSections = {};
  async function read() {
    pending = true;
    try {
      do {
        dirty = false;
        const requestEpoch = epoch;
        try {
          const reply = await getLogs(cursor);
          if (closed) return;
          if (epoch !== requestEpoch) continue;
          const next = {...sections};
          for (const key of Object.keys(reply.sections) as LogKey[]) {
            const patch = reply.sections[key]!;
            next[key] = [...(patch.reset ? [] : (sections[key] || []).slice(patch.remove)), ...patch.lines];
          }
          sections = next;
          cursor = reply.cursor;
          changed({sections, loading: false, error: ""});
        } catch {
          if (!closed && epoch === requestEpoch) changed({sections, loading: false, error: m.logsUnavailable});
        }
      } while (dirty && !closed && currentState().connection === "connected");
    } finally {pending = false;}
  }
  function refresh() {
    if (currentState().connection !== "connected") {
      ++epoch;
      cursor = null;
      sections = {};
      dirty = false;
      changed({sections, loading: false, error: m.backendUnavailable});
      return;
    }
    dirty = true;
    if (!pending) void read();
  }
  const unsubscribe = subscribeState(refresh);
  refresh();
  return () => {closed = true; unsubscribe();};
}

export function usePortalLogs() {
  const [view, setView] = useState<LogView>({sections: {}, loading: true, error: ""});
  useEffect(() => watchLogs(setView), []);
  return view;
}
