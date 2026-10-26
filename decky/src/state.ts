import { useSyncExternalStore } from "react";
import { addEventListener, removeEventListener, callable } from "@decky/api";
import { ActionId, State } from "./types";
import { messages as m } from "./messages";

const getState = callable<[], State>("get_state");
const reconnectClient = callable<[], State>("reconnect");
export const requestAction = callable<[ActionId, Record<string, string>], {accepted: boolean; error: string}>("request_action");
export const updatesViewed = callable<[string], {accepted: boolean}>("updates_viewed");
export const updatePlugin = callable<[string], {accepted: boolean; error: string}>("update_plugin");
export const maintenanceViewed = callable<[string, string, string, number], {accepted: boolean}>("maintenance_viewed");
const inspectInstallation = callable<[], State>("refresh_installation");

let state: State = {connection: "connecting", snapshot: null, request_pending: false, error: "", revision: -1};
const listeners = new Set<() => void>();
let generation = 0;
export const currentState = () => state;
export function subscribeState(listener: () => void) {
  listeners.add(listener);
  return () => {listeners.delete(listener);};
}
function changed(next: State) {
  if (!next || typeof next.revision !== "number" || next.revision < state.revision) return;
  state = next;
  listeners.forEach(listener => listener());
}
export function usePortalState(): State {
  return useSyncExternalStore(subscribeState, currentState);
}
export function startState() {
  const run = ++generation;
  state = {connection: "connecting", snapshot: null, request_pending: false, error: "", revision: -1};
  const listener = addEventListener<[State]>("portal_state", next => {if (run === generation) changed(next);});
  void getState().then(next => {if (run === generation) changed(next);}).catch(() => {
    if (run === generation && state.revision === -1) changed({...state, connection: "unavailable", error: m.backendUnavailable, revision: 0});
  });
  return () => {++generation; removeEventListener("portal_state", listener); listeners.clear();};
}
export async function reconnect() {
  const run = generation;
  const next = await reconnectClient();
  if (run === generation) changed(next);
}
export async function refreshInstallation() {
  const run = generation;
  const next = await inspectInstallation();
  if (run === generation) changed(next);
}
