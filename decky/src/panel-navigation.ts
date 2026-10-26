import { useLayoutEffect, useRef } from "react";

/** Decky reuses the plugin-list scroller. Enter each Portal page at its start. */
export function usePanelEntry(visible: boolean, page: string | boolean, fillViewport = false) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!visible || !root.current) return;
    const view = root.current.ownerDocument.defaultView;
    if (!view) return;
    const element = root.current;
    const measure = () => {
      const height = `${Math.max(0, view.innerHeight - element.getBoundingClientRect().top)}px`;
      if (element.style.getPropertyValue("--portal-panel-height") !== height)
        element.style.setProperty("--portal-panel-height", height);
    };
    const frame = view.requestAnimationFrame(() => {
      const element = root.current;
      if (!element) return;
      const first = element.querySelector<HTMLElement>('button:not(:disabled), [role="button"]:not([aria-disabled="true"])');
      first?.focus({preventScroll: true});
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const overflow = parent.ownerDocument.defaultView?.getComputedStyle(parent).overflowY;
        if (parent.scrollTop > 0 || ((overflow === "auto" || overflow === "scroll") && parent.scrollHeight > parent.clientHeight)) {
          parent.scrollTop = 0;
          break;
        }
      }
      if (fillViewport) measure();
    });
    const observer = fillViewport ? new ResizeObserver(measure) : null;
    observer?.observe(element);
    if (fillViewport) view.addEventListener("resize", measure);
    return () => {
      view.cancelAnimationFrame(frame);
      observer?.disconnect();
      if (fillViewport) view.removeEventListener("resize", measure);
      if (fillViewport) element.style.removeProperty("--portal-panel-height");
    };
  }, [visible, page, fillViewport]);
  return root;
}
