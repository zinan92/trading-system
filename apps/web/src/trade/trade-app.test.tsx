/* #1415: /trade is one React tree. The skeleton must be the pre-#1415 page node for node, and the
 * islands must mount into it the way the bridge mounted them. */
import {flushSync} from "react-dom";
import {createRoot} from "react-dom/client";
import reference from "./skeleton.reference.html?raw";
import {Skeleton} from "./Skeleton";
import {createIslands} from "./Islands";
import {loadInOrder, startTradeApp} from "./TradeApp";
import {islands} from "../registry";

const NO_TEXT = new Set(["SELECT", "TABLE", "TBODY", "THEAD", "TR", "COLGROUP"]);

/** Tag, attributes (sorted) and text, exactly; whitespace-only text where it cannot render is skipped. */
function shape(node: Node): unknown {
  if (node.nodeType === Node.TEXT_NODE) return NO_TEXT.has(node.parentNode?.nodeName ?? "") && !node.textContent?.trim() ? null : ["#text", node.textContent];
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const el = node as Element;
  // style compares as the browser parsed it ("padding:0" and "padding: 0px;" are the same rule)
  const attrs = [...el.attributes].map((a): [string, string] => [a.name, a.name === "style" ? (el as HTMLElement).style.cssText : a.value]).sort((x, y) => x[0].localeCompare(y[0]));
  // A form control's current value is what the page shows; React sets value/checked as properties.
  const live = el instanceof HTMLInputElement ? [["value", el.value], ["checked", String(el.checked)]]
    : el instanceof HTMLTextAreaElement ? [["value", el.value]] : el instanceof HTMLSelectElement ? [["value", el.value]] : [];
  return [el.tagName, attrs, live, [...el.childNodes].map(shape).filter(x => x !== null)];
}

const consoleErrors: unknown[][] = [];
beforeEach(() => { consoleErrors.length = 0; vi.spyOn(console, "error").mockImplementation((...args) => { consoleErrors.push(args); }); });
afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ""; });

test("the skeleton is the pre-#1415 page, node for node", () => {
  const expected = document.createElement("div");
  expected.innerHTML = reference;
  const actual = document.createElement("div");
  document.body.append(actual);
  flushSync(() => createRoot(actual).render(<Skeleton />));
  expect([...actual.childNodes].map(shape)).toEqual([...expected.childNodes].map(shape));
  expect(consoleErrors).toEqual([]);  // no React warning (nesting, keys, controlled inputs)
});

test("islands wait for boot, clear the legacy children, and follow data-props synchronously", async () => {
  const host = document.createElement("div");
  host.innerHTML = '<div id="a" data-island="echo" data-props=\'{"text":"one"}\'><span class="legacy">旧</span></div><div data-island="nope"></div>';
  document.body.append(host);
  const echo = ({text}: {text: string}) => <b>{text}</b>;
  const {Islands, boot} = createIslands({echo});
  const root = document.createElement("div");
  document.body.append(root);
  flushSync(() => createRoot(root).render(<Islands />));
  const el = host.querySelector<HTMLElement>("#a")!;
  expect(el.innerHTML).toBe('<span class="legacy">旧</span>');  // before boot GridMind's legacy markup stays
  expect(el.hasAttribute("data-island-live")).toBe(false);
  vi.spyOn(console, "warn").mockImplementation(() => {});
  expect(boot(host)).toBe(1);  // the unknown "nope" is left alone
  expect(el.innerHTML).toBe("<b>one</b>");
  expect(el.hasAttribute("data-island-live")).toBe(true);
  expect(root.childNodes.length).toBe(0);  // portals: nothing is drawn where the tree is mounted
  el.dataset.props = '{"text":"two"}';
  await new Promise(resolve => setTimeout(resolve, 0));  // the MutationObserver callback renders synchronously
  expect(el.innerHTML).toBe("<b>two</b>");
  el.dataset.props = "{bad";
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(el.innerHTML).toBe("<b>two</b>");  // a bad data-props keeps what is on screen
});

test("scripts load in order and the islands mount only after the last one ran", async () => {
  const appended: string[] = [];
  const parent = {appendChild: (script: HTMLScriptElement) => { appended.push(script.src); expect(script.async).toBe(false); if (appended.length === 3) queueMicrotask(() => script.onerror?.(new Event("error"))); return script; }} as unknown as Node;
  await loadInOrder(["/a.js", "/b.js", "/c.js"], parent);  // a failed last script still lets the page boot
  expect(appended.map(src => new URL(src).pathname)).toEqual(["/a.js", "/b.js", "/c.js"]);
});

test("the desk page boots the tree, then the islands, with the real registry", async () => {
  const container = document.createElement("div");
  container.id = "trade-app";
  container.dataset.boot = "[]";
  container.innerHTML = "<p>页面脚本没有加载</p>";
  document.body.append(container);
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));
  let after = 0;
  const {ready} = startTradeApp(container, islands, () => { after += 1; });
  expect(container.textContent).not.toContain("页面脚本没有加载");  // React replaced the fallback line
  expect(container.querySelector("#gmStatus #envBadge")?.textContent).toBe("PAPER");  // legacy header until islands mount
  expect(container.querySelector("[data-island-live]")).toBeNull();
  await ready;
  expect(after).toBe(1);
  expect(container.querySelectorAll("[data-island]").length).toBe(container.querySelectorAll("[data-island-live]").length);
  expect(container.querySelector("#gmStatus #envBadge")?.textContent).toBe("PAPER");  // now drawn by GmHeader
});
