export type ActionId = "update" | "restart" | "channel" | "rollback" | "cancel_rollback" | "cancel_pending" | "clear_cache";
export interface Target {
  available?: boolean; token?: string; reason?: string; identity?: string;
  version?: string; image?: string; current_track?: string; target_track?: string;
  state?: string; error?: string;
}
export interface Deployment {
  role?: string; identity?: string; version?: string; image?: string; staged?: boolean;
}
export interface Module {
  id: string; name: string; status: string; detail?: string; error?: string;
  fraction?: number | null;
  source?: string;
}
export interface Snapshot {
  available?: boolean; running?: boolean; request_busy?: boolean; operation?: string;
  run?: string; result_viewed_run?: string; result?: string; stage?: string; progress_percent?: number | null;
  deployment?: string; deployment_known?: boolean; deployment_label?: string; error?: string;
  modules?: Module[]; module_plan?: Module[];
  rollback_state?: string; rollback_error?: string;
  cancel_state?: string; cancel_available?: boolean; cancel_error?: string;
  channel_state?: string; channel_error?: string;
  cache_available?: boolean; cache_token?: string; cache_reason?: string;
  cache_state?: string; cache_error?: string;
  management?: {
    known?: boolean; error?: string; summary?: {known?: boolean; transaction?: unknown; engine?: string; entries?: Deployment[]};
    cancel_target?: Target; channel_status?: Target;
  };
  [key: string]: unknown;
}
export interface State {
  connection: "connecting" | "connected" | "reconnecting" | "unavailable" | "not_installed";
  snapshot: Snapshot | null;
  request_pending: boolean;
  error: string;
  revision: number;
  pending_action?: ActionId | "";
  rollback_cancel_target?: Target;
  installation?: {
    decky_available?: boolean; loader_available?: boolean; installed_version?: string; bundled_version?: string;
    can_install?: boolean; can_update?: boolean; can_downgrade?: boolean; bundle_token?: string; installing?: boolean;
    prompt_requested?: boolean; error?: string; reason?: string;
  } | null;
  installation_error?: string;
  installation_pending?: boolean;
}
export interface Action {
  id: ActionId; label: string; reason: string; title: string; body: string;
  target: Record<string, string>;
}
export type LogKey = "logs" | "rollback_logs" | "cancel_logs" | "channel_logs" | "cache_logs";
export type LogSections = Partial<Record<LogKey, string[]>>;
export interface LogReply {
  cursor: string;
  sections: Partial<Record<LogKey, {reset: boolean; remove: number; lines: string[]}>>;
}
