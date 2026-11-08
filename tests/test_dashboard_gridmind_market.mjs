import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import {gridmindSource} from "./gridmind_page.mjs";

const html = gridmindSource(); // #1420: the page and its own files, inlined back

function sourceBetween(name, nextName) {
  const start = html.indexOf(`function ${name}`);
  const end = html.indexOf(`\nfunction ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `could not extract ${name}`);
  return html.slice(start, end);
}

// #1416: mergeMarketBars, retainLastTrustedMarket and reconcileReadModelMarket moved to
// apps/web/src/gridmind/read.ts; their tests moved with them (read.test.ts, "market snapshots").
const shouldLoadOlderHistory = vm.runInNewContext(
  `(${sourceBetween("shouldLoadOlderHistory", "historyInputCanLoad")})`,
);
const historyInputCanLoad = vm.runInNewContext(
  `(${sourceBetween("historyInputCanLoad", "setupHistoryGesture")})`,
);

test("history loads when a right drag starts at the oldest fitted edge", () => {
  assert.equal(shouldLoadOlderHistory({startX: 100, lastX: 120, startRange: {from: -1}}, {from: -1}), true);
  assert.equal(shouldLoadOlderHistory({startX: 100, lastX: 108, startRange: {from: -1}}, {from: -1}), false);
  assert.equal(shouldLoadOlderHistory({startX: 100, lastX: 120, startRange: {from: 100}}, {from: 100}), false);
  assert.equal(shouldLoadOlderHistory({startX: 100, lastX: 120, startRange: {from: 20}}, {from: 10}), true);
});

test("wheel or trackpad input loads history only at the oldest edge", () => {
  assert.equal(historyInputCanLoad({from: 20}, 2500, {from: 24}, 2000), true);
  assert.equal(historyInputCanLoad({from: -2}, 2500, {from: -1}, 2000), true);
  assert.equal(historyInputCanLoad({from: 25}, 2500, {from: 30}, 2000), false);
  assert.equal(historyInputCanLoad({from: 24}, 2500, {from: 20}, 2000), false);
  assert.equal(historyInputCanLoad({from: 24}, 2500, {from: 24}, 2000), false);
  assert.equal(historyInputCanLoad({from: 20}, 1500, {from: 24}, 2000), false);
});
