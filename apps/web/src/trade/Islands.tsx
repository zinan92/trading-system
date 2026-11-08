/* The page's islands as children of the one /trade tree (#1415). Each island renders through a portal
 * into its placeholder in the skeleton, exactly where the bridge used to mount a root of its own:
 *  - nothing mounts until boot (after GridMind's scripts and gm-desk.js ran), as the bridge did;
 *  - the placeholder's legacy children are removed and it is marked data-island-live first, as
 *    createRoot did (GridMind stops writing inside it from then on);
 *  - a changed data-props re-renders synchronously, so GridMind never shows numbers and the button
 *    beside them out of step (#1329). */
import {useLayoutEffect, useState, useSyncExternalStore, type ComponentType} from "react";
import {createPortal, flushSync} from "react-dom";

export type Registry = Record<string, ComponentType<any>>;

function readProps(el: HTMLElement, name: string): Record<string, unknown> | null {
  try {
    return JSON.parse(el.dataset.props || "{}") as Record<string, unknown>;
  } catch {
    console.warn(`[desk-web] ${name} 的 data-props 不是 JSON`);
    return null;
  }
}

function IslandPortal({el, name, Component}: {el: HTMLElement; name: string; Component: ComponentType<any>}) {
  const [props, setProps] = useState(() => readProps(el, name));
  useLayoutEffect(() => {
    const observer = new MutationObserver(() => {
      const next = readProps(el, name);
      if (next) flushSync(() => setProps(next));  // a bad data-props keeps what is on screen, as the bridge did
    });
    observer.observe(el, {attributes: true, attributeFilter: ["data-props"]});
    return () => observer.disconnect();
  }, [el, name]);
  return props ? createPortal(<Component {...props} />, el) : null;
}

type Placed = {el: HTMLElement; name: string; Component: ComponentType<any>};

export function createIslands(registry: Registry) {
  let placed: Placed[] = [];
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

  /** Mount every placeholder inside scope; returns how many. Unknown names are left alone, as the bridge did. */
  function boot(scope: ParentNode): number {
    const found: Placed[] = [];
    scope.querySelectorAll<HTMLElement>("[data-island]").forEach(el => {
      const name = el.dataset.island ?? "";
      const Component = registry[name];
      if (!Component) { console.warn(`[desk-web] 未知组件 ${name}`); return; }
      found.push({el, name, Component});
    });
    for (const {el} of found) { el.replaceChildren(); el.setAttribute("data-island-live", ""); }
    flushSync(() => { placed = found; listeners.forEach(listener => listener()); });
    return found.length;
  }

  function Islands() {
    const current = useSyncExternalStore(subscribe, () => placed);
    return <>{current.map(({el, name, Component}, index) => <IslandPortal key={index} el={el} name={name} Component={Component} />)}</>;
  }

  return {boot, Islands, mounted: () => placed.length};
}
