/** Keep decisions in parity with Portal's ui/application.py and ui/maintenance.py. */
import { Action, Snapshot, State, Target } from "./types";
import { format, messages as m } from "./messages";

const kinds = ["rollback", "cancel", "layer", "cache", "channel", "image", "delete"];
const text = (value: unknown, fallback: string = m.unknown): string => typeof value === "string" && value.trim() ? value.slice(0, 256) : fallback;
const token = (target?: Target) => typeof target?.token === "string" && /^[a-f0-9]{64}$/.test(target.token);

export function busy(s: Snapshot): boolean {
  const transaction = s.management?.summary?.transaction;
  return Boolean(s.running || s.request_busy || (Array.isArray(transaction) ? transaction.length : transaction)
    || kinds.some(kind => s[kind + "_state"] === "running"));
}

export function actions(state: State): Action[] {
  const s = state.snapshot || {};
  const base = state.connection !== "connected" ? m.unavailable : state.request_pending || busy(s) ? m.anotherOperation : "";
  const entries = s.management?.summary?.entries || [];
  const saved = entries.find(e => e.role === "Saved deployment");
  const next = entries.find(e => e.role === "Next restart");
  const cancellation = state.rollback_cancel_target;
  const cancelling = cancellation?.available === true && Boolean(next);
  const rollback = cancelling ? next : saved;
  let rollbackReason = base;
  if (!rollbackReason) {
    if (!s.management?.summary?.known) rollbackReason = m.unknownPrevious;
    else if (!rollback) rollbackReason = m.noPrevious;
    else if (!rollback.identity || rollback.identity.toLowerCase() === "unknown") rollbackReason = m.verify;
    else if (next && !cancelling) rollbackReason = m.prepared;
    else if (cancelling && !token(cancellation)) rollbackReason = m.verify;
  }
  const pending = s.management?.cancel_target;
  let pendingReason = base;
  if (!pendingReason) {
    if (s.cancel_state === "unknown") pendingReason = text(s.cancel_error, m.verify);
    else if (!s.cancel_available || pending?.available !== true) pendingReason = text(pending?.reason, m.noPending);
    else if (!s.deployment_known || !s.deployment || pending.identity !== s.deployment || !token(pending)) pendingReason = m.verify;
  }
  const channel = s.management?.channel_status;
  let cacheReason = base;
  if (!cacheReason) {
    if (s.management && s.management.known !== true) cacheReason = m.managementUnknown;
    else if (s.cache_available !== true) cacheReason = text(s.cache_reason, text(s.cache_error, m.cacheUnavailable));
    else if (["unknown", "unavailable"].includes(s.cache_state || "")) cacheReason = text(s.cache_error, m.cacheUnknown);
    else if (!token({token: s.cache_token})) cacheReason = m.verify;
  }
  const targetTrack = channel?.target_track;
  const track = targetTrack === "testing" ? "Testing" : "Stable";
  let channelReason = base;
  if (!channelReason) {
    if (channel?.state === "unknown") channelReason = text(channel.error, m.verify);
    else if (channel?.available !== true) channelReason = text(channel?.reason, m.channelUnavailable);
    else if (!["stable", "testing", "unstable"].includes(channel.current_track || "")
      || !["stable", "testing"].includes(targetTrack || "")) channelReason = m.channelUnknown;
    else if (!token(channel)) channelReason = m.verify;
  }
  const returning = channel?.current_track === targetTrack;
  const restartReady = Boolean(s.deployment && s.deployment_known);
  return [
    {id: restartReady ? "restart" : "update", label: s.running && s.operation === "update" ? m.updating : base ? m.busy : restartReady ? m.restart : m.update,
      reason: base || (!s.available ? m.unavailable : s.deployment && !s.deployment_known ? m.verify : ""),
      title: restartReady ? m.restartTitle : "", body: restartReady ? m.restartBody : "", target: restartReady ? {deployment: s.deployment!} : {}},
    {id: cancelling ? "cancel_rollback" : "rollback", label: cancelling ? m.cancelRollback : m.rollback,
      reason: rollbackReason, title: cancelling ? m.cancelRollbackTitle : m.rollbackTitle,
      body: format(cancelling ? m.cancelRollbackBody : m.rollbackBody, {version: text(rollback?.version)}),
      target: cancelling ? {token: cancellation?.token || "", identity: text(rollback?.identity, "")}
        : {identity: text(rollback?.identity, ""), role: text(rollback?.role, "")}},
    {id: "channel", label: format(returning ? m.channelReturn : m.channelLabel, {track}), reason: channelReason,
      title: format(returning ? m.channelReturnTitle : m.channelTitle, {target_name: `Bazzite ${track}`}),
      body: format(returning ? m.channelReturnBody : m.channelBody,
        {target_name: `Bazzite ${track}`, current_track: text(channel?.current_track).replace(/^./, c => c.toUpperCase()), version: text(channel?.version), image: text(channel?.image)}),
      target: {token: channel?.token || ""}},
    {id: "cancel_pending", label: m.cancelPending, reason: pendingReason, title: m.cancelPendingTitle,
      body: format(m.cancelPendingBody, {version: text(pending?.version), image: text(pending?.image)}), target: {token: pending?.token || ""}},
    {id: "clear_cache", label: m.clearCache, reason: cacheReason, title: m.clearCacheTitle,
      body: m.clearCacheBody, target: {token: s.cache_token || ""}},
  ];
}

export function sameAction(a: Action, b: Action | undefined): boolean {
  return Boolean(b && a.id === b.id && !b.reason && JSON.stringify(a.target) === JSON.stringify(b.target));
}

export function bootedIdentity(s: Snapshot | null) {
  const running = s?.management?.summary?.entries?.find(e => e.role === "Running now");
  return {bootedVersion: running?.version || "", channel: s?.management?.channel_status?.current_track || "",
    image: running?.image || ""};
}
