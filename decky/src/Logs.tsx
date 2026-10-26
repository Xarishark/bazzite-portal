import { useEffect, useRef } from "react";
import { Focusable, GamepadButton, Navigation } from "@decky/ui";
import { usePortalLogs } from "./log-reader";
import { messages as m } from "./messages";
import { styles } from "./styles";
import { usePortalState } from "./state";

export const LOGS_ROUTE = "/bazzite-portal/logs";
export function Logs() {
  const {sections: logs, loading, error} = usePortalLogs();
  const {snapshot} = usePortalState();
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const lines = snapshot?.operation === "update" ? logs.logs : undefined;
  useEffect(() => {following.current = true;}, [snapshot?.run]);
  useEffect(() => {
    if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [lines]);
  return <Focusable {...{focusable: true, autoFocus: true}} preferredFocus className="portal-logs-page"
    actionDescriptionMap={{[GamepadButton.OK]: null}}
    onCancelButton={event => {event.preventDefault(); event.stopPropagation(); Navigation.NavigateBack();}}
    onCancelActionDescription={m.back}
    onGamepadDirection={event => {
      if (![GamepadButton.DIR_UP, GamepadButton.DIR_DOWN].includes(event.detail.button)) return;
      event.preventDefault(); event.stopPropagation();
      scroll.current?.scrollBy(0, event.detail.button === GamepadButton.DIR_DOWN ? 100 : -100);
    }}>
    <style>{styles}</style><h1>{m.logs}</h1>
    {error ? <div role="alert">{error}</div> : null}
    <div ref={scroll} className="portal-logs-scroll" role="log" aria-live="off"
      onScroll={() => {const node = scroll.current; if (node) following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;}}>
      {lines?.length ? lines.join("\n") : loading ? m.loading : error ? null : m.noLogs}
    </div>
  </Focusable>;
}
