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
  </Focusable>;
}
