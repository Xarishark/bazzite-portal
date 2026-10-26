import logo from "../../bazzite_portal/assets/indicator/bazzite-logo.svg?raw";

// Decky's menu icons inherit the current foreground color. Reuse the logo's
// actual B/controller paths, omitting its colored background rather than adding
// a second, hand-maintained drawing or changing Portal's original artwork.
const paths = (logo.match(/<path\b[^>]*\/>/g) || []).slice(1).join("")
  .replace(/fill="white"/g, 'fill="currentColor"')
  .replace(/fill-opacity="[^"]*"/g, "");

export function BazziteIcon() {
  return <svg width="1em" height="1em" viewBox="32 32 536 536" fill="currentColor"
    aria-hidden="true" focusable="false" dangerouslySetInnerHTML={{ __html: paths }} />;
}
