import { useLayoutEffect, useRef } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { currentPage, setPage, usePanelState } from "./panel-state";
import { messages as m } from "./messages";
import { usePortalState } from "./state";

export function Title() {
  const {page} = usePanelState();
  const change = "";
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    // Loader owns the Back button immediately before its titleView slot. Capture
    // that button only while one of our subpages is open; never patch Loader.
    const slot = root.current, header = slot?.parentElement;
    if (!slot || !header || page === "main") return;
    const back = (event: MouseEvent) => {
      const target = event.target as Element | null;
      const button = target?.closest('button, [role="button"]');
      if (!button || !header.contains(button) || slot.contains(button)) return;
      event.preventDefault(); event.stopImmediatePropagation(); setPage("main");
    };
    header.addEventListener("click", back, true);
    return () => header.removeEventListener("click", back, true);
  }, [page]);
  return <Focusable ref={root} {...{focusable: false}} style={{display: "flex", alignItems: "center", flex: 1, minWidth: 0}}
    onCancel={event => {if (currentPage() === "main") return false; event.preventDefault(); event.stopPropagation(); setPage("main"); return true;}}>
    <div style={{flex: 1}}>{change ? m.app : page === "updates" ? m.updates : page === "settings" ? m.settings : m.app}</div>
    {page === "main" && !change ? <DialogButton style={{height: "28px", width: "40px", minWidth: 0, padding: "6px 10px"}}
      onOKActionDescription={m.settings} onClick={() => setPage("settings")}>
      <svg aria-label={m.settings} viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="m10 2-.6 3-2 .9L5 4.5 2.5 9l2.4 1.6v2.8L2.5 15 5 19.5l2.4-1.4 2 .9.6 3h4l.6-3 2-.9 2.4 1.4 2.5-4.5-2.4-1.6v-2.8L21.5 9 19 4.5l-2.4 1.4-2-.9L14 2Z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
    </DialogButton> : null}
  </Focusable>;
}
