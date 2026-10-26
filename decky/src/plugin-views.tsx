import { useSyncExternalStore } from "react";
import type { ComponentType } from "react";

type Views = {panel: ComponentType; title: ComponentType};
type Host = {views: Views | null; listeners: Set<() => void>};
const key = Symbol.for("bazzite-portal.plugin-views");
// Loader retains activePlugin.content during replacement. Keep only our view
// host shared across module loads so that retained element can mount fresh code.
const owner = globalThis as typeof globalThis & {[key]?: Host};
const host = owner[key] ??= {views: null, listeners: new Set()};
const subscribe = (listener: () => void) => {
  host.listeners.add(listener);
  return () => {host.listeners.delete(listener);};
};
const snapshot = () => host.views;

export function publishViews(views: Views) {
  host.views = views;
  host.listeners.forEach(listener => listener());
}

export function PluginPanel() {
  const views = useSyncExternalStore(subscribe, snapshot);
  const Panel = views?.panel;
  return Panel ? <Panel /> : null;
}

export function PluginTitle() {
  const views = useSyncExternalStore(subscribe, snapshot);
  const Title = views?.title;
  return Title ? <Title /> : null;
}
