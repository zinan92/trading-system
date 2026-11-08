// dashboard-gridmind.html with its own files inlined back (#1420); see tests/gridmind_page.py.
import {readFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OWN_FILE = /<link rel="stylesheet" href="(assets\/gridmind\/[\w.-]+\.css)">|<script src="(assets\/gridmind\/[\w.-]+\.js)"><\/script>/g;

export function gridmindSource() {
  return readFileSync(resolve(root, "dashboard-gridmind.html"), "utf8").replace(OWN_FILE, (_match, css, js) =>
    css ? `<style>${readFileSync(resolve(root, css), "utf8")}</style>` : `<script>${readFileSync(resolve(root, js), "utf8")}</script>`);
}
