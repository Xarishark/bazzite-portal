import { ConfirmModal, showModal } from "@decky/ui";
import { Action } from "./types";
import { requestAction } from "./state";
import { messages as m } from "./messages";

const modals = new Set<{Close(): void}>();
export function closeOperationModals() {for (const modal of modals) modal.Close(); modals.clear();}
export function showReason(title: string, reason: string) {showModal(<ConfirmModal bAlertDialog strTitle={title} strDescription={reason} strOKButtonText={m.ok} />);}
export function openOperation(action: Action, _inspect: boolean) {
  const modal = showModal(<ConfirmModal strTitle={action.title} strDescription={action.body} strOKButtonText={action.label} strCancelButtonText={m.cancel}
    onOK={() => {void requestAction(action.id, action.target);}} />, undefined, {fnOnClose: () => modals.delete(modal)});
  modals.add(modal);
}
