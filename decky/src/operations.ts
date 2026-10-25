import { ActionId, Snapshot, State } from "./types";
import { messages as m } from "./messages";

export type Kind = "rollback" | "cancel" | "cache" | "channel";
export type OperationKey = [string, string, number];
export function kindFor(action: ActionId): Kind | null {
  return action === "rollback" || action === "cancel_rollback" ? "rollback"
    : action === "cancel_pending" ? "cancel" : action === "clear_cache" ? "cache" : action === "channel" ? "channel" : null;
}
export const sameKey = (a: OperationKey, b: OperationKey) => a.every((value, i) => value === b[i]);
export function operation(snapshot: Snapshot | null, kind: Kind) {
  const s = snapshot || {}, generations = s.maintenance_generations as Record<string, number> | undefined;
  const key: OperationKey = [String(s[kind + "_unit"] || ""), String(s[kind + "_invocation"] || ""), generations?.[kind] || 0];
  const status = String(s[kind + "_state"] || "unavailable");
  const terminal = ["success", "failed", "unknown"].includes(status);
  const viewed = Array.isArray(s.maintenance_viewed) && s.maintenance_viewed.some((value: unknown) =>
    Array.isArray(value) && value[0] === kind && sameKey(value.slice(1) as OperationKey, key));
  const cancelRollback = kind === "rollback" && key[0].startsWith("bazzite-portal-maintenance@cancel-rollback-");
  const titles = kind === "rollback" ? cancelRollback
    ? [m.cancelRollback, m.rollbackCancelling, m.rollbackCancelled, m.rollbackCancelFailed]
    : [m.rollback, m.rollbackRunning, m.rollbackDone, m.rollbackFailed]
    : kind === "cancel" ? [m.cancelPending, m.cancelRunning, m.cancelDone, m.cancelFailed]
    : kind === "cache" ? [m.clearCache, m.cacheRunning, m.cacheDone, m.cacheFailed]
    : [m.rebase, m.channelRunning, m.channelDone, m.channelFailed];
  return {key, status, terminal, viewed, title: titles[0],
    label: status === "running" ? titles[1] : status === "success" ? titles[2] : status === "unknown" ? m.statusUnknown : titles[3],
    error: String(s[kind + "_error"] || ""), logError: String(s[kind + "_log_error"] || ""),
    known: Boolean(key[0] && Number.isSafeInteger(key[2]) && key[2] > 0)};
}
export function rowOperation(state: State, action: ActionId) {
  const kind = kindFor(action);
  if (!kind) return null;
  const op = operation(state.snapshot, kind);
  const pending = Boolean(state.request_pending && kindFor(state.pending_action as ActionId) === kind);
  return {...op, kind, running: op.status === "running" || pending,
    inspect: op.known && (op.status === "running" || (op.terminal && !op.viewed))};
}
