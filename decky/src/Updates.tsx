import { useEffect, useState } from "react";
import { DialogButton, Focusable, PanelSection, PanelSectionRow, ProgressBar } from "@decky/ui";
import { Action, Module, State } from "./types";
import { bootedIdentity } from "./actions";
import { messages as m } from "./messages";
import { LOGS_ROUTE } from "./Logs";
import { Throbber } from "./Throbber";
import { BazziteIcon } from "./BazziteIcon";
import { updatesViewed } from "./state";
import checkIcon from "../../bazzite_portal/assets/icons/hicolor/scalable/actions/bazzite-portal-circle-check-symbolic.svg?raw";
import failedIcon from "../../bazzite_portal/assets/icons/hicolor/scalable/actions/bazzite-portal-circle-x-symbolic.svg?raw";

// The shared worker acknowledges confirmed success/failure only. Unknown history
// can still be dismissed for this plugin session without changing that evidence.
let dismissedUnknownRun = "";

export function UpdateButton({label, disabled, busy, unavailable, onClick}: {
  label: string; disabled?: boolean; busy?: boolean; unavailable?: boolean; onClick: () => void;
}) {
  return <div className="portal-update-button portal-action" data-unavailable={unavailable}><DialogButton className="portal-panel-button" disabled={disabled}
    onClick={onClick}><span className="portal-action-label"><span className="portal-update-label">
      <span className="portal-update-symbol"><BazziteIcon /></span><span>{label}</span>
    </span>{busy ? <Throbber /> : null}</span></DialogButton></div>;
}

function ComponentProgress({step, deploymentReady}: {step: Module; deploymentReady: boolean}) {
  const fraction = typeof step.fraction === "number" && Number.isFinite(step.fraction) && step.fraction >= 0 && step.fraction <= 1
    ? step.fraction : null;
  const running = step.status === "running";
  const name = step.id === "system" ? m.system : step.name;
  const noSystemUpdate = step.id === "system" && step.status === "skipped" && step.detail === m.systemCurrent;
  const success = step.status === "success" || noSystemUpdate;
  const failed = step.status === "failed";
  const finished = step.status === "finished";
  const icon = success || finished ? checkIcon : failed ? failedIcon : "";
  const detail = step.status === "finished" ? m.finished : step.status === "pending" ? m.waiting
    : step.status === "success" && ["system", "brew", "flatpak"].includes(step.id)
      ? step.id === "system" && deploymentReady ? m.restartRequired : m.updated
    : noSystemUpdate ? m.systemCurrent : step.detail
      || (step.status === "failed" ? m.failed : step.status === "skipped" ? m.skipped : m.statusUnknown);
  const subtitle = step.error || detail;
  return <div className="portal-panel-component">
    <div className="portal-step-content">
      <strong title={name}>{name}</strong>
      <span className="portal-step-detail" role={step.error ? "alert" : undefined} title={subtitle}>{subtitle}</span>
      {running && fraction !== null ? <ProgressBar nProgress={fraction * 100} /> : null}
    </div>
  </div>;
}

export function Updates({state, action, sending, visible, onUpdate, onBack, showLogs, feedback = "", navigate}: {
  state: State; action: Action; sending: boolean; onUpdate: () => void;
  showLogs: boolean; feedback?: string; visible: boolean; onBack: () => void; navigate: (path: string) => void;
}) {
  const snapshot = state.snapshot;
  const updateRun = snapshot?.operation === "update";
  const runningUpdate = updateRun && Boolean(snapshot?.running);
  const startingUpdate = sending || (state.request_pending && state.pending_action === "update");
  const blockedReason = !runningUpdate && !startingUpdate ? action.reason : "";
  const notice = !runningUpdate && !startingUpdate ? feedback || blockedReason : "";
  const [visibleRun, setVisibleRun] = useState("");
  const run = updateRun ? snapshot?.run || "" : "";
  const completed = Boolean(run && !runningUpdate && ["success", "failed", "unknown"].includes(snapshot?.result || ""));
  const dismissed = false;
  const identity = bootedIdentity(snapshot);
  const steps: Module[] = updateRun ? snapshot?.modules || [] : [];
  return <Focusable {...{focusable: false}} flow-children="column" onCancelActionDescription={m.back}
    onCancel={event => {event.preventDefault(); event.stopPropagation(); onBack(); return true;}}>
    <PanelSection>
      <PanelSectionRow><UpdateButton label={startingUpdate ? m.requestPending : action.label}
        unavailable={Boolean(action.reason) || startingUpdate} busy={runningUpdate || startingUpdate}
        onClick={() => {if (runningUpdate || startingUpdate || blockedReason) return; onUpdate();}} /></PanelSectionRow>
      {notice ? <PanelSectionRow><div className="portal-panel-status" role="status">{notice}</div></PanelSectionRow> : null}
      {steps.filter(step => !(step.status === "skipped" && step.source === "config")).map(step =>
        <PanelSectionRow key={step.id}><ComponentProgress step={step} deploymentReady={Boolean(snapshot?.deployment)} /></PanelSectionRow>)}
      {runningUpdate && !steps.length ? <PanelSectionRow><div className="portal-panel-activity"><Throbber /><span>{snapshot?.stage || m.updating}</span></div></PanelSectionRow> : null}
      {snapshot && completed && !dismissed ? <PanelSectionRow><div className="portal-panel-status">{snapshot.result === "success" ? m.succeeded : snapshot.result === "unknown" ? m.statusUnknown : m.operationFailed}</div></PanelSectionRow> : null}
      {showLogs ? <PanelSectionRow><DialogButton className="portal-panel-button" disabled={!snapshot} onClick={() => navigate(LOGS_ROUTE)}>{m.logs}</DialogButton></PanelSectionRow> : null}
    </PanelSection>
  </Focusable>;
}
