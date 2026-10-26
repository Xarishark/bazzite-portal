import { useSyncExternalStore } from "react";
import { callable } from "@decky/api";
import type { ArtworkTarget } from "./steam-artwork";

export type Page = "main" | "updates" | "settings";
let view = {page: "main" as Page, showLogs: false};
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {listeners.add(listener); return () => {listeners.delete(listener);};};
const publish = () => listeners.forEach(listener => listener());
export const usePanelState = () => useSyncExternalStore(subscribe, () => view);
export const currentPage = () => view.page;
export function setPage(page: Page) {view = {...view, page}; publish();}
const read = callable<[], {show_logs: boolean}>("get_preferences");
const save = callable<[boolean], {show_logs: boolean}>("set_show_logs");
export async function loadPreferences() {const result = await read(); view = {...view, showLogs: result.show_logs}; publish();}
export async function setShowLogs(enabled: boolean) {const result = await save(enabled); view = {...view, showLogs: result.show_logs}; publish();}
export const addToSteam = callable<[], {accepted: boolean; already_added?: boolean; artwork_ready?: boolean; artwork_targets?: ArtworkTarget[]; error?: string}>("add_to_steam");

export interface AutoUpdateStatus {visible: boolean; known: boolean; enabled: boolean; reason?: string}
type AutoUpdateReply = ({accepted: true} & AutoUpdateStatus) | {accepted: false; error: string};
export const getAutoUpdates = callable<[], AutoUpdateReply>("get_auto_updates");
export const setAutoUpdates = callable<[boolean], AutoUpdateReply>("set_auto_updates");
