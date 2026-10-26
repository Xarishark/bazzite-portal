import { messages as m } from "./messages";

export function Throbber() {
  return <span className="portal-throbber" role="status" aria-label={m.updating} />;
}
