/** Same-version/channel lookup using identity supplied by Portal's worker only. */
import { message, messages } from "./messages";

const API = "https://api.github.com/repos/ublue-os/bazzite/releases";
const WEB_PREFIX = "https://github.com/ublue-os/bazzite/releases/tag/";
const VERSION = /^(?:(?:testing|stable)-)?v?(\d{2,4}\.\d{8}(?:\.\d+)?)$/i;
const MAX_PAGE_BYTES = 1024 * 1024;
const MAX_BODY_BYTES = 64 * 1024;
const TIMEOUT_MS = 8000;
const PAGE_SIZE = 30;
const MAX_PAGES = 3;

export interface ReleaseNotesData {
  title: string;
  body: string;
}

type Release = Record<string, unknown>;

function clean(value: unknown, limit: number): string {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, limit) : "";
}

export function releaseMatches(release: unknown, version: string, channel: string): release is Release {
  if (release === null || typeof release !== "object" || Array.isArray(release)) return false;
  const candidate = release as Release;
  if (candidate.draft === true || typeof candidate.tag_name !== "string") return false;
  const actual = VERSION.exec(candidate.tag_name)?.[1];
  const baseVersion = version.split(".").slice(0, 2).join(".");
  if (actual !== version && actual !== baseVersion) return false;
  const text = [candidate.tag_name, candidate.name].filter(part => typeof part === "string")
    .join(" ").toLowerCase();
  return channel === "testing" ? text.includes("testing")
    : channel === "stable" && (text.includes("stable") || candidate.tag_name.toLowerCase().startsWith("stable-"));
}

async function readPage(page: number, signal?: AbortSignal): Promise<unknown[]> {
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(`${API}?per_page=${PAGE_SIZE}&page=${page}`, {
      signal: controller.signal, headers: { Accept: "application/vnd.github+json" },
    });
    if (!response.ok) throw new Error(message(messages.httpFailed, { status: response.status }));
    if (Number(response.headers.get("content-length")) > MAX_PAGE_BYTES) throw new Error(messages.oversized);
    let text: string;
    if (response.body) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parts: string[] = [];
      let size = 0;
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          size += chunk.value.byteLength;
          if (size > MAX_PAGE_BYTES) {
            await reader.cancel();
            throw new Error(messages.oversized);
          }
          parts.push(decoder.decode(chunk.value, { stream: true }));
        }
        parts.push(decoder.decode());
        text = parts.join("");
      } finally {
        reader.releaseLock();
      }
    } else {
      text = await response.text();
      if (new TextEncoder().encode(text).byteLength > MAX_PAGE_BYTES) throw new Error(messages.oversized);
    }
    const data: unknown = JSON.parse(text);
    if (!Array.isArray(data)) throw new Error(messages.invalidList);
    return data;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export async function fetchReleaseNotes(version: string, channel: string, signal?: AbortSignal): Promise<ReleaseNotesData> {
  const normalizedVersion = VERSION.exec(version)?.[1];
  const normalizedChannel = channel.toLowerCase();
  if (!normalizedVersion || !["stable", "testing"].includes(normalizedChannel)) throw new Error(messages.unknownIdentity);
  try {
    for (let page = 1; page <= MAX_PAGES; page += 1) {
      const releases = await readPage(page, signal);
      for (const release of releases) {
        if (!releaseMatches(release, normalizedVersion, normalizedChannel)) continue;
        const expectedUrl = WEB_PREFIX + encodeURIComponent(release.tag_name as string);
        if (release.html_url !== expectedUrl) continue;
        const body = typeof release.body === "string"
          ? release.body.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "").trim() : "";
        return {
          title: clean(release.name, 256) || clean(release.tag_name, 256),
          body: new TextDecoder().decode(new TextEncoder().encode(body).slice(0, MAX_BODY_BYTES)),
        };
      }
      if (releases.length < PAGE_SIZE) break;
    }
  } catch (error) {
    if (signal?.aborted) throw error;
    const detail = error instanceof Error ? clean(error.message, 1200) : messages.failed;
    throw new Error(message(messages.lookupFailed, { error: detail }));
  }
  throw new Error(messages.missing);
}
