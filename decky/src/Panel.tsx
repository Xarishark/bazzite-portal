import { useEffect, useRef, useState } from "react";
import { useQuickAccessVisible } from "@decky/api";
import { ButtonItem, ConfirmModal, DialogButton, Navigation, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { Action } from "./types";
import { actions, sameAction } from "./actions";
import { currentState, reconnect, refreshInstallation, requestAction, updatePlugin, usePortalState } from "./state";
import { messages as m } from "./messages";
import { loadPreferences, setPage, usePanelState } from "./panel-state";
import { UpdateButton, Updates } from "./Updates";
import { usePanelEntry } from "./panel-navigation";
import { openOperation, showReason } from "./OperationModal";
import { rowOperation } from "./operations";
import { Throbber } from "./Throbber";
import { styles } from "./styles";

function navigate(path: string) {Navigation.CloseSideMenus(); Navigation.Navigate(path);}
export function Panel() {
  const state = usePortalState();
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [awaitingUpdate, setAwaitingUpdate] = useState(false);
  const requestInFlight = useRef(false);
  const expectedUpdate = useRef<{run: string; revision: number} | null>(null);
  const {page, showLogs} = usePanelState();
  const updatesOpen = page === "updates";
  const visible = useQuickAccessVisible();
  const change = "";
  const previousChange = useRef(change);
  const root = usePanelEntry(visible, change || page, Boolean(change));
  const controls = actions(state);
  const connected = state.connection === "connected";
  useEffect(() => {return () => setPage("main");}, []);
  useEffect(() => {if (visible && connected) void refreshInstallation().catch(() => {});}, [visible, connected]);
  useEffect(() => {
    if ((change || previousChange.current) && page !== "main") setPage("main");
    previousChange.current = change;
  }, [change, page]);
  useEffect(() => {
    const expected = expectedUpdate.current, snapshot = state.snapshot;
    if (!expected) return;
    if (state.connection !== "connected" || (snapshot?.operation === "update" && (snapshot.running || (snapshot.run && snapshot.run !== expected.run)))
        || (state.error && state.revision > expected.revision && !state.request_pending)) {
      expectedUpdate.current = null; setAwaitingUpdate(false);
    }
  }, [state]);
  const installation = state.installation;
  async function execute(action: Action) {
    if (requestInFlight.current || (action.id === "update" && expectedUpdate.current)) return;
    const current = actions(currentState()).find(candidate => candidate.id === action.id);
    if (!sameAction(action, current)) {
      if (action.id === "update" && currentState().snapshot?.operation === "update" && currentState().snapshot?.running) return;
      setError(current?.reason || m.changed); return;
    }
    requestInFlight.current = true;
    if (action.id === "update") {expectedUpdate.current = {run: currentState().snapshot?.run || "", revision: currentState().revision}; setAwaitingUpdate(true);}
    setSending(true); setError("");
    try {
      const result = await requestAction(action.id, action.target);
      if (!result.accepted) {
        expectedUpdate.current = null; setAwaitingUpdate(false);
        if (!(action.id === "update" && currentState().snapshot?.operation === "update" && currentState().snapshot?.running)) setError(result.error || m.requestFailed);
      }
    } catch {expectedUpdate.current = null; setAwaitingUpdate(false); setError(m.requestFailed);}
    finally {requestInFlight.current = false; setSending(false);}
  }
  function confirm(action: Action) {
    const active = rowOperation(currentState(), action.id);
    if (active?.running || active?.inspect) {openOperation(action, true); return;}
    if (action.reason || sending) {showReason(action.label, action.reason || m.requestPending); return;}
    if (action.id === "update") {void execute(action); return;}
    if (action.id === "restart") {
      showModal(<ConfirmModal strTitle={action.title} strDescription={action.body} strOKButtonText={m.restart}
        strCancelButtonText={m.cancel} onOK={() => {void execute(action);}} />);
      return;
    }
    openOperation(action, false);
  }
  const statusText = state.connection === "not_installed" ? m.notInstalled : state.connection === "connecting" ? m.connecting
    : state.connection === "reconnecting" ? m.reconnecting : state.error || m.backendUnavailable;
  return <div ref={root} className="portal-panel">
    <style>{styles}</style>
    {page === "main" ? <PanelSection><PanelSectionRow><UpdateButton label={m.update}
      onClick={() => setPage("updates")} /></PanelSectionRow></PanelSection> : null}
    {!connected ? <PanelSection title={m.app}>
      <PanelSectionRow><div className="portal-panel-status" role="status">{statusText}</div></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => {void reconnect().catch(() => setError(m.backendUnavailable));}}>{m.reconnect}</ButtonItem></PanelSectionRow>
    </PanelSection> : null}
    {!updatesOpen && (error || connected && state.error) ? <PanelSection><PanelSectionRow><div className="portal-panel-status" role="alert">{error || state.error}</div></PanelSectionRow></PanelSection> : null}
    {updatesOpen ? <Updates state={state} action={controls[0]} sending={sending || awaitingUpdate} visible={visible}
      feedback={error || (connected ? state.error : "")}
      onUpdate={() => confirm(controls[0])} onBack={() => setPage("main")} showLogs={showLogs} navigate={navigate} /> : <>
    <PanelSection title={m.management}>
      {controls.slice(1).map(action => {
        const active = rowOperation(state, action.id);
        const unavailable = Boolean(action.reason || sending) && !active?.running && !active?.inspect;
        return <PanelSectionRow key={action.id}>
          <div className="portal-action" data-unavailable={unavailable}>
            <DialogButton className="portal-panel-button" onClick={() => confirm(action)}>
              <span className="portal-action-label"><span>{active?.running ? active.status === "running" ? active.label : m.requestPending : action.label}</span>
                {active?.running ? <Throbber /> : active?.inspect && active.terminal ? <span className={active.status === "success" ? "portal-result-icon-success" : "portal-result-icon-failed"}>{active.status === "success" ? "✓" : "!"}</span> : null}
              </span>
            </DialogButton>
          </div>
        </PanelSectionRow>;
      })}
    </PanelSection>
    </>}
  </div>;
}
