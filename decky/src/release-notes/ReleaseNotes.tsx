/** Adapted from Bazzite Buddy fix/release-notes; see ../../NOTICE.md. */
import { useEffect, useRef, useState } from "react";
import { Focusable, GamepadButton, Navigation, SteamSpinner } from "@decky/ui";
import remarkHtml from "remark-html";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import { unified } from "unified";
import { fetchReleaseNotes } from "./fetch";
import { messages } from "./messages";
import { releaseNotesStyles } from "./styles";

export const RELEASE_NOTES_ROUTE = "/bazzite-portal/release-notes";
// Steam supports autoFocus, but @decky/ui 4.9.2 omits it from its types.
const readingFocus = { focusable: true, autoFocus: true };

export interface ReleaseNotesProps {
  bootedVersion: string;
  channel: string;
  image?: string;
  onBack?: () => void;
}

export function ReleaseNotes({ bootedVersion, channel, image, onBack }: ReleaseNotesProps) {
  const [notes, setNotes] = useState<{ title: string; html: string } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const scroll = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setNotes(null);
    setError("");
    async function load() {
      try {
        const release = await fetchReleaseNotes(bootedVersion, channel, controller.signal);
        // remark-html sanitizes raw HTML by default. Do not enable sanitize:false.
        const html = await unified().use(remarkParse).use(remarkGfm).use(remarkHtml)
          .process(release.body || messages.empty);
        if (!controller.signal.aborted) {
          setNotes({ title: release.title, html: String(html) });
          scroll.current?.scrollTo(0, 0);
        }
      } catch (err) {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : messages.failed);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [bootedVersion, channel, revision]);

  return (
    <Focusable {...readingFocus} preferredFocus className="bazzite-portal-notes-page"
      actionDescriptionMap={{ [GamepadButton.OK]: null }}
      onGamepadDirection={event => {
        const direction = event.detail.button;
        if (direction !== GamepadButton.DIR_UP && direction !== GamepadButton.DIR_DOWN) return;
        event.preventDefault();
        event.stopPropagation();
        scroll.current?.scrollBy(0, direction === GamepadButton.DIR_DOWN ? 100 : -100);
      }}
      onSecondaryButton={event => {
        event.preventDefault();
        event.stopPropagation();
        if (!loading) setRevision(value => value + 1);
      }}
      onSecondaryActionDescription={loading ? messages.loadingAction : messages.refresh}
      onCancelButton={event => {
        event.preventDefault();
        event.stopPropagation();
        if (onBack) onBack();
        else Navigation.NavigateBack();
      }} onCancelActionDescription={messages.back}>
      <style>{releaseNotesStyles}</style>
      <header className="bazzite-portal-notes-toolbar">
        <h1>{messages.title}</h1>
        {image && <small>{image}</small>}
      </header>
      <div ref={scroll} role="region" aria-label={messages.region}
        className="bazzite-portal-notes-scroll"
        onClick={event => {
          const element = event.target instanceof Element ? event.target : null;
          const link = element?.closest("a");
          if (link) {
            event.preventDefault();
            if (/^https?:\/\//i.test(link.href)) Navigation.NavigateToExternalWeb(link.href);
          }
        }}>
        {loading ? <div className="bazzite-portal-notes-status"><SteamSpinner /><p>{messages.loading}</p></div>
          : error ? <div className="bazzite-portal-notes-status" role="alert">
            <p>{error}</p><p>{messages.retry}</p>
          </div>
          : <>
            <h2>{notes?.title}</h2>
            <article className="bazzite-portal-notes-body" dangerouslySetInnerHTML={{ __html: notes?.html || "" }} />
          </>}
      </div>
    </Focusable>
  );
}
