import { RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { DialogBody, DialogButton, DialogFooter, DialogHeader, Focusable, GamepadButton, ModalRoot, showModal, ConfirmModal } from "@decky/ui";
import { Action, LogKey } from "./types";
import { actions, sameAction } from "./actions";
import { currentState, maintenanceViewed, requestAction, usePortalState } from "./state";
import { usePortalLogs } from "./log-reader";
import { Kind, OperationKey, kindFor, operation, sameKey } from "./operations";
import { messages as m } from "./messages";
import { styles } from "./styles";
import { Throbber } from "./Throbber";

const modals = new Set<{Close(): void}>();
export function openOperation(action: Action, inspect: boolean) {
  const modal = showModal(<OperationModal action={action} inspect={inspect} />, undefined,
    {fnOnClose: () => modals.delete(modal)});
  modals.add(modal);
}
export function closeOperationModals() {for (const modal of [...modals]) modal.Close(); modals.clear();}

export function showReason(title: string, reason: string) {
  showModal(<ConfirmModal bAlertDialog strTitle={title} strDescription={reason} strOKButtonText={m.ok} />);
}

function ProgressLogs({kind, operationKey, scroll}: {kind: Kind; operationKey: OperationKey; scroll: RefObject<HTMLDivElement>}) {
  const {sections, loading, error} = usePortalLogs();
  const following = useRef(true);
  const lines = sections[(kind + "_logs") as LogKey] || [];
  // A modal bound to a previous invocation must never display a newer run's logs.
  const current = operation(currentState().snapshot, kind);
  const matches = sameKey(current.key, operationKey);
  useEffect(() => {if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;}, [lines]);
  return <><div ref={scroll} className="portal-operation-logs" role="log"
    onScroll={() => {const node = scroll.current; if (node) following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;}}>
    {matches ? lines.length ? lines.join("\n") : loading ? m.loading : m.noLogs : m.newerOperation}</div>
    {error ? <div role="alert">{error}</div> : null}</>;
}

export function OperationModal({action, inspect, closeModal = () => {}}: {action: Action; inspect: boolean; closeModal?: () => void}) {
  const state = usePortalState(), kind = kindFor(action.id)!;
  const op = operation(state.snapshot, kind);
  const baseline = useRef(op.key);
  const [phase, setPhase] = useState(inspect ? "progress" : "confirm");
  const [binding, setBinding] = useState<OperationKey | null>(inspect && op.known && (op.key[1] || op.terminal) && !(state.request_pending && kindFor(state.pending_action as Action["id"]) === kind && op.status !== "running") ? op.key : null);
  const [failure, setFailure] = useState("");
  const seen = useRef<OperationKey | null>(null);
  const logScroll = useRef<HTMLDivElement>(null);
  const footerButton = useRef<HTMLDivElement>(null);
  const starting = useRef(false);
  const requestRevision = useRef(state.revision);
  const newInvocation = Boolean(op.key[1] && op.key[1] !== baseline.current[1]);
  const failedRequest = !op.key[1] && ["failed", "unknown"].includes(op.status);
  const fresh = op.known && (newInvocation || failedRequest)
    && (op.key[2] > baseline.current[2] || (inspect && op.key[0] === baseline.current[0] && op.key[2] === baseline.current[2]))
    && !sameKey(op.key, baseline.current);
  const bound = binding || (phase === "progress" && fresh ? op.key : null);
  const matches = Boolean(bound && sameKey(op.key, bound));
  const connected = state.connection === "connected";
  const done = connected && matches && op.terminal;
  const failed = Boolean(failure || (done && op.status !== "success"));
  useEffect(() => {if (!binding && bound) setBinding(bound);}, [binding, bound]);
  useEffect(() => {seen.current = done ? bound : null;}, [done, bound]);
  useLayoutEffect(() => {
    if (phase !== "progress") return;
    const button = footerButton.current, view = button?.ownerDocument.defaultView;
    if (!button || !view) return;
    const frame = view.requestAnimationFrame(() => button.focus({preventScroll: true}));
    return () => view.cancelAnimationFrame(frame);
  }, [phase]);
  useEffect(() => () => {
    const key = seen.current, current = operation(currentState().snapshot, kind);
    if (key && currentState().connection === "connected" && current.terminal && sameKey(current.key, key))
      void maintenanceViewed(kind, ...key).catch(() => {});
  }, [kind]);
  useEffect(() => {
    if (phase === "progress" && !bound && !state.request_pending && state.error && state.revision > requestRevision.current) setFailure(state.error);
  }, [phase, bound, state.request_pending, state.error, state.revision]);

  async function start() {
    if (starting.current) return;
    const latest = actions(currentState()).find(candidate => candidate.id === action.id);
    if (!sameAction(action, latest)) {setFailure(latest?.reason || m.changed); setPhase("progress"); return;}
    starting.current = true; requestRevision.current = currentState().revision; baseline.current = operation(currentState().snapshot, kind).key;
    setPhase("progress");
    try {const reply = await requestAction(action.id, action.target); if (!reply.accepted) setFailure(reply.error || m.requestFailed);}
    catch {setFailure(m.requestFailed);}
  }
  const confirmLabel = action.id === "channel" ? m.rebase : action.id === "cancel_pending" ? m.cancelDeployment : action.id === "clear_cache" ? m.clearCacheConfirm : action.label;
  return <ModalRoot onCancel={closeModal} onEscKeypress={closeModal} closeModal={closeModal}>
    <style>{styles}</style>
    <Focusable {...{focusable: false}} flow-children="column" className="portal-operation-modal"
      onCancelActionDescription={m.back} onCancel={event => {event.preventDefault(); event.stopPropagation(); closeModal(); return true;}}
      onGamepadDirection={event => {
        if (phase !== "progress" || ![GamepadButton.DIR_UP, GamepadButton.DIR_DOWN].includes(event.detail.button)) return;
        event.preventDefault(); event.stopPropagation();
        logScroll.current?.scrollBy(0, event.detail.button === GamepadButton.DIR_DOWN ? 100 : -100);
        return true;
      }}>
      <DialogHeader>{phase === "confirm" ? action.title : op.title}</DialogHeader>
      <DialogBody>{phase === "confirm" ? action.body : <>
        <div className="portal-operation-status">{!done && !failure && connected ? <Throbber /> : null}
          <span>{failure || (!connected ? m.backendUnavailable : bound && !matches ? m.newerOperation : done || matches ? op.label : m.requestPending)}</span></div>
        {matches && op.error ? <div role="alert">{op.error}</div> : null}
        {matches && op.logError ? <div role="alert">{op.logError}</div> : null}
        {bound && matches ? <ProgressLogs kind={kind} operationKey={bound} scroll={logScroll} /> : null}
      </>}</DialogBody>
      <DialogFooter className="portal-operation-footer-wrap"><Focusable {...{focusable: false}} flow-children="row"
        data-single={phase !== "confirm"} className={"portal-operation-footer " + (done || failure ? failed ? "portal-result-failed" : "portal-result-complete" : "")}>
        {phase === "confirm" ? <>
          <DialogButton className="portal-operation-button" preferredFocus onClick={closeModal}>{m.cancel}</DialogButton>
          <DialogButton className="portal-operation-button" onClick={() => {void start();}}>{confirmLabel}</DialogButton>
        </> : <DialogButton ref={footerButton} preferredFocus className="portal-operation-button" onClick={closeModal}>
          {done || failure ? failed ? !failure && op.status === "unknown" ? m.statusUnknown : m.failed : m.completed : m.back}
        </DialogButton>}
      </Focusable></DialogFooter>
    </Focusable>
  </ModalRoot>;
}
