import { useEffect, useState } from "react";
import { ConfirmModal, DialogButton, Focusable, PanelSection, PanelSectionRow, showModal, ToggleField } from "@decky/ui";
import { addToSteam, getAutoUpdates, setAutoUpdates, setPage, setShowLogs, usePanelState } from "./panel-state";
import type { AutoUpdateStatus } from "./panel-state";
import { usePortalState } from "./state";
import { format, messages as m } from "./messages";
import { version } from "../package.json";

export function Settings({visible = true}: {visible?: boolean}) {
  const {showLogs} = usePanelState();
  const connected = usePortalState().connection === "connected";
  const [saving, setSaving] = useState(false), [adding, setAdding] = useState(false);
  const [message, setMessage] = useState("");
  const [autoUpdates, setAutoUpdatesState] = useState<AutoUpdateStatus | null>(null);
  const [autoLoading, setAutoLoading] = useState(false), [autoSaving, setAutoSaving] = useState(false);
  const [autoError, setAutoError] = useState("");
  return <Focusable {...{focusable: false}} flow-children="column" onCancelActionDescription={m.back}
    onCancel={event => {event.preventDefault(); event.stopPropagation(); setPage("main"); return true;}}>
    <PanelSection>
      <PanelSectionRow><ToggleField label={m.showLogs} checked={showLogs} disabled={saving} bottomSeparator="none"
        onChange={enabled => {setSaving(true); void setShowLogs(enabled).catch(() => setMessage(m.settingsFailed)).finally(() => setSaving(false));}} /></PanelSectionRow>
      {message ? <PanelSectionRow><div role="status">{message}</div></PanelSectionRow> : null}
      <PanelSectionRow><div className="portal-settings-info">
        <div>{format(m.pluginVersion, {version})}</div>
        <div>{format(m.pluginMadeBy, {authors: "Xarishark • Zacharias Xenakis"})}</div>
      </div></PanelSectionRow>
    </PanelSection>
  </Focusable>;
}
