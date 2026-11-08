/* /trade as one React tree (#1415): the skeleton renders once, then GridMind's scripts and gm-desk.js
 * start in their old order, then the islands mount into the tree. Behaviour is unchanged; only who
 * draws the page's structure is. */
import {memo} from "react";
import {flushSync} from "react-dom";
import {createRoot} from "react-dom/client";
import {Skeleton} from "./Skeleton";
import {createIslands, type Registry} from "./Islands";

// No props, no state, no context: React never reconciles the skeleton again, so the legacy scripts can
// write inside it and move its cards (gm-desk.js placeForViewport) without React noticing.
const Frame = memo(function Frame() {
  return <Skeleton />;
});

/** Load scripts one after another, in order; resolves when the last has run or failed. A failed script
 * does not stop the next, as with the plain tags it replaces. */
export function loadInOrder(urls: string[], parent: Node = document.body): Promise<void> {
  return new Promise(resolve => {
    if (!urls.length) { resolve(); return; }
    urls.forEach((url, index) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = false;
      if (index === urls.length - 1) script.onload = script.onerror = () => resolve();
      parent.appendChild(script);
    });
  });
}

export function startTradeApp(container: HTMLElement, registry: Registry, afterIslands: () => void = () => {}) {
  const islands = createIslands(registry);
  const {Islands} = islands;
  const root = createRoot(container);
  flushSync(() => root.render(<><Frame /><Islands /></>));
  let boot: string[] = [];
  try { boot = JSON.parse(container.dataset.boot || "[]") as string[]; } catch { console.warn("[desk-web] data-boot 不是 JSON"); }
  const ready = loadInOrder(boot).then(() => { islands.boot(container); afterIslands(); });
  return {root, islands, ready};
}
