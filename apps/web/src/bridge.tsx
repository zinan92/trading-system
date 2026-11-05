/* Island bridge: the legacy page writes <div data-island="name" data-props='{json}'>, this mounts
 * the matching React component there. New placeholders are found by a MutationObserver, a changed
 * data-props re-renders, and a placeholder that leaves the page is unmounted (its timers stop). */
import type {ComponentType} from "react";
import {createRoot, type Root} from "react-dom/client";

export type Registry = Record<string, ComponentType<any>>;

export function createBridge(registry: Registry) {
  const roots = new Map<Element, Root>();

  function render(el: HTMLElement) {
    const name = el.dataset.island ?? "";
    const Component = registry[name];
    if (!Component) {
      console.warn(`[desk-web] 未知组件 ${name}`);
      return;
    }
    let props: Record<string, unknown>;
    try {
      props = JSON.parse(el.dataset.props || "{}") as Record<string, unknown>;
    } catch {
      console.warn(`[desk-web] ${name} 的 data-props 不是 JSON`);
      return;
    }
    let root = roots.get(el);
    if (!root) { root = createRoot(el); roots.set(el, root); }
    root.render(<Component {...props} />);
  }

  function scan(scope: ParentNode) {
    if (scope instanceof HTMLElement && scope.dataset.island && !roots.has(scope)) render(scope);
    scope.querySelectorAll<HTMLElement>("[data-island]").forEach(el => { if (!roots.has(el)) render(el); });
  }

  function sweep() {
    for (const [el, root] of roots) if (!el.isConnected) { roots.delete(el); root.unmount(); }
  }

  function start(target: Node = document.body) {
    scan(target as ParentNode);
    const observer = new MutationObserver(records => {
      for (const record of records) {
        if (record.type === "attributes" && record.target instanceof HTMLElement && roots.has(record.target)) render(record.target);
        record.addedNodes.forEach(node => { if (node instanceof HTMLElement) scan(node); });
      }
      sweep();
    });
    observer.observe(target, {childList: true, subtree: true, attributes: true, attributeFilter: ["data-props"]});
    return observer;
  }

  return {start, scan, sweep, mounted: () => roots.size};
}
