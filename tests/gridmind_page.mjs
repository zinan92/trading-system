// /trade as one text, with GridMind's own files inlined (#1420; the page itself since #1419); see tests/gridmind_page.py.
import {readFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PAGE = resolve(root, "apps/trading-desk/src/trading_desk/trade.html");
const MARKUP = resolve(root, "apps/web/src/trade/skeleton.reference.html");
const OWN_CSS = /<link rel="stylesheet" href="(assets\/gridmind\/[\w.-]+\.css)">/g;
const MOUNT = /<div id="trade-app"[^>]*data-boot='([^']*)'[^>]*>[\s\S]*?<\/div>/;  // its child is the no-JS fallback line

export function gridmindSource() {
  const template = readFileSync(PAGE, "utf8");
  const boot = JSON.parse(template.match(MOUNT)[1]).filter(path => path.startsWith("assets/gridmind/"));
  const scripts = boot.map(path => `<script>${readFileSync(resolve(root, path), "utf8")}</script>\n`).join("");
  const body = `<div id="trade-app">${readFileSync(MARKUP, "utf8")}</div>\n${scripts}`;
  return template.replace(OWN_CSS, (_match, css) => `<style>${readFileSync(resolve(root, css), "utf8")}</style>`).replace(MOUNT, () => body);
}
