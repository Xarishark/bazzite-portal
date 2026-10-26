import { version } from "../package.json";
import type { State } from "./types";

/** The running bundle's own version also rejects stale pre-reload inventory. */
export function pluginChange(state: State): "update" | "downgrade" | null {
  const installation = state.installation;
  if (!installation?.bundled_version || installation.bundled_version === version) return null;
  if (installation.can_update) return "update";
  if (installation.can_downgrade) return "downgrade";
  return null;
}
