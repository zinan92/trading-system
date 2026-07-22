import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {fileURLToPath} from "node:url";
import {dirname, resolve} from "node:path";
import vm from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(resolve(root, "dashboard-gridmind.html"), "utf8");

function sourceBetween(name, nextName) {
  const start = html.indexOf(`function ${name}`);
  const end = html.indexOf(`\nfunction ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `could not extract ${name}`);
  return html.slice(start, end);
}

const mergeMarketBars = vm.runInNewContext(
  `(${sourceBetween("mergeMarketBars", "marketDatasetKey")})`,
);
const shouldLoadOlderHistory = vm.runInNewContext(
  `(${sourceBetween("shouldLoadOlderHistory", "historyInputCanLoad")})`,
);
const historyInputCanLoad = vm.runInNewContext(
  `(${sourceBetween("historyInputCanLoad", "setupHistoryGesture")})`,
);
const retainLastTrustedMarket = vm.runInNewContext(
  `(${sourceBetween("retainLastTrustedMarket", "acceptMarketSnapshot")})`,
);
const isFresh = (market) => market?.trusted === true;
const acceptMarketSnapshot = vm.runInNewContext(
  `(${sourceBetween("acceptMarketSnapshot", "reconcileReadModelMarket")})`,
  {isFresh, mergeMarketBars, retainLastTrustedMarket, state: {market: null}},
);
const reconcileStart = html.indexOf("function reconcileReadModelMarket");
const reconcileEnd = html.indexOf("\nasync function loadOlderMarketBars", reconcileStart);
assert.ok(reconcileStart >= 0 && reconcileEnd > reconcileStart);
const reconcileReadModelMarket = vm.runInNewContext(
  `(${html.slice(reconcileStart, reconcileEnd)})`,
  {isFresh, acceptMarketSnapshot},
);

test("failed refresh retains candles but removes execution trust", () => {
  const trusted = {
    status: "ready",
    trusted: true,
    fresh: true,
    symbol: "GOLD",
    timeframe: "1m",
    latest_timestamp: "2026-07-21T08:00:00+00:00",
    bars: [{timestamp: "2026-07-21T08:00:00+00:00", close: 4000}],
  };

  const retained = retainLastTrustedMarket(trusted, {
    status: "blocked",
    access_issues: ["upstream timeout"],
  });

  assert.equal(retained.trusted, false);
  assert.equal(retained.fresh, false);
  assert.equal(retained.retained_last_trusted, true);
  assert.deepEqual(retained.bars, trusted.bars);
  assert.deepEqual(retained.access_issues, ["upstream timeout"]);
});

test("polling cannot overwrite retained trusted candles with a blocked read model", () => {
  const trusted = {
    status: "ready",
    trusted: true,
    fresh: true,
    symbol: "GOLD",
    timeframe: "30m",
    latest_timestamp: "2026-07-21T08:00:00+00:00",
    bars: [{timestamp: "2026-07-21T08:00:00+00:00", close: 4000}],
  };
  const retained = retainLastTrustedMarket(trusted, {
    status: "blocked",
    bars: [],
    access_issues: ["upstream timeout"],
  });

  const afterPoll = reconcileReadModelMarket(retained, {
    status: "blocked",
    trusted: false,
    fresh: false,
    bars: [],
    access_issues: ["still blocked"],
  });

  assert.equal(afterPoll.retained_last_trusted, true);
  assert.equal(afterPoll.trusted, false);
  assert.deepEqual(afterPoll.bars, trusted.bars);
  assert.deepEqual(afterPoll.access_issues, ["still blocked"]);
});

test("historical prepend deduplicates timestamps and preserves live authority", () => {
  const history = {
    status: "ready",
    trusted: false,
    trusted_history: true,
    symbol: "GOLD",
    timeframe: "1m",
    bars: [
      {timestamp: "2026-07-21T07:58:00+00:00", close: 3998},
      {timestamp: "2026-07-21T07:59:00+00:00", close: 3999},
    ],
  };
  const live = {
    status: "ready",
    trusted: true,
    fresh: true,
    symbol: "GOLD",
    timeframe: "1m",
    latest_timestamp: "2026-07-21T08:00:00+00:00",
    bars: [
      {timestamp: "2026-07-21T07:59:00+00:00", close: 3999},
      {timestamp: "2026-07-21T08:00:00+00:00", close: 4000},
    ],
  };

  const merged = mergeMarketBars(history, live);

  assert.equal(merged.trusted, true);
  assert.equal(merged.latest_timestamp, "2026-07-21T08:00:00+00:00");
  assert.equal(
    JSON.stringify(merged.bars.map((row) => row.timestamp)),
    JSON.stringify([
      "2026-07-21T07:58:00+00:00",
      "2026-07-21T07:59:00+00:00",
      "2026-07-21T08:00:00+00:00",
    ]),
  );
});

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
