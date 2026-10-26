/** English presentation text. Keep identifiers and worker data out of messages. */
export const messages = {
  title: "Bazzite release notes",
  region: "Release notes",
  loading: "Loading release notes…",
  loadingAction: "Loading…",
  refresh: "Refresh",
  back: "Back",
  retry: "Press X to retry.",
  empty: "No release notes were provided for this release.",
  unknownIdentity: "Version or release channel is unknown.",
  missing: "No release notes match this version and channel.",
  failed: "Could not load release notes.",
  lookupFailed: "Release notes lookup failed: {error}",
  httpFailed: "GitHub returned HTTP {status}. Please try again later.",
  invalidList: "GitHub release response was not a list.",
  oversized: "GitHub release response exceeded the size limit.",
} as const;

export function message(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{([a-zA-Z_]+)\}/g, (placeholder, name: string) =>
    Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : placeholder);
}
