import { callable, toaster } from "@decky/api";
import { currentState, subscribeState } from "./state";
import { BazziteIcon } from "./BazziteIcon";
import { messages as m } from "./messages";

const claim = callable<[string], {notify: boolean}>("claim_update_notification");

/** One Steam notification per automatic invocation, driven by worker events. */
export function startNotifications() {
  const attempted = new Set<string>();
  let closed = false;
  function changed() {
    const state = currentState(), snapshot = state.snapshot;
    const run = snapshot?.run;
    if (closed || state.connection !== "connected" || snapshot?.operation !== "update"
        || !snapshot.running || snapshot.service !== "uupd.service"
        || !snapshot.modules?.some(step => step.source === "journal" && ["running", "finished", "success"].includes(step.status))
        || !run || !/^[0-9a-f]{32}$/.test(run) || attempted.has(run)) return;
    attempted.add(run);
    if (attempted.size > 32) attempted.delete(attempted.values().next().value!);
    void claim(run).then(result => {
      const latest = currentState();
      if (!closed && result.notify && latest.connection === "connected"
          && latest.snapshot?.run === run && latest.snapshot.running
          && latest.snapshot.operation === "update" && latest.snapshot.service === "uupd.service") {
        toaster.toast({title: m.app, body: m.autoUpdateStarted, icon: <BazziteIcon />});
      }
    }).catch(() => {});
  }
  const unsubscribe = subscribeState(changed);
  changed();
  return () => {closed = true; unsubscribe();};
}
