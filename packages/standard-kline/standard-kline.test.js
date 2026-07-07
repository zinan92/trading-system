const test = require("node:test");
const assert = require("node:assert/strict");
const kline = require("./standard-kline.js");

test("adaptBarPayload converts timestamps to epoch seconds and preserves provider metadata", () => {
  const payload = {
    schema_version: "ohlcv-v1",
    status: "ready",
    source_mode: "rest_poll",
    symbol: "BTCUSD",
    timeframe: "1m",
    provider: "example_exchange",
    quality_flags: [],
    is_synthetic: false,
    bars: [
      { timestamp: "2026-01-01T00:00:00Z", open: 100, high: 102, low: 99, close: 101, volume: 5 },
      { timestamp: "2026-01-01T00:01:00Z", open: 101, high: 104, low: 100, close: 103, volume: 8 },
    ],
  };
  const result = kline.adaptBarPayload(payload);
  assert.equal(result.candles.length, 2);
  assert.equal(result.candles[0].time, Math.floor(Date.parse("2026-01-01T00:00:00Z") / 1000));
  assert.equal(result.meta.provider, "example_exchange");
  assert.equal(result.meta.is_synthetic, false);
  assert.deepEqual(result.volumes.map(v => v.value), [5, 8]);
});

test("adaptBarPayload drops rows missing required OHLC fields instead of throwing", () => {
  const payload = {
    bars: [
      { timestamp: "2026-01-01T00:00:00Z", open: 100, high: 102, low: 99, close: 101 },
      { timestamp: "2026-01-01T00:01:00Z", open: 101, high: 104 }, // missing low/close
      { open: 1, high: 2, low: 0, close: 1 }, // missing timestamp
    ],
  };
  const result = kline.adaptBarPayload(payload);
  assert.equal(result.candles.length, 1);
});

test("adaptBarPayload flags synthetic data via an explicit is_synthetic flag", () => {
  const result = kline.adaptBarPayload({
    is_synthetic: true,
    bars: [{ timestamp: "2026-01-01T00:00:00Z", open: 1, high: 1, low: 1, close: 1 }],
  });
  assert.equal(result.meta.is_synthetic, true);
});

test("adaptBarPayload flags synthetic data via a provider/source_mode substring match", () => {
  const result = kline.adaptBarPayload({
    provider: "synthetic_seed:demo",
    bars: [{ timestamp: "2026-01-01T00:00:00Z", open: 1, high: 1, low: 1, close: 1 }],
  });
  assert.equal(result.meta.is_synthetic, true);
});

test("adaptBarPayload only treats custom quality_flags as synthetic when the caller opts in", () => {
  const payload = {
    quality_flags: ["demo_only"],
    bars: [{ timestamp: "2026-01-01T00:00:00Z", open: 1, high: 1, low: 1, close: 1 }],
  };
  const withoutOptIn = kline.adaptBarPayload(payload);
  assert.equal(withoutOptIn.meta.is_synthetic, false);

  const withOptIn = kline.adaptBarPayload(payload, { syntheticFlags: ["demo_only"] });
  assert.equal(withOptIn.meta.is_synthetic, true);
});

test("isSyntheticMeta is exposed standalone and honors the same rules as adaptBarPayload", () => {
  assert.equal(kline.isSyntheticMeta(null), true);
  assert.equal(kline.isSyntheticMeta({ provider: "real_feed" }), false);
  assert.equal(kline.isSyntheticMeta({ quality_flags: ["demo"] }, ["demo"]), true);
  assert.equal(kline.isSyntheticMeta({ quality_flags: ["demo"] }), false);
});

test("clampLogicalRange keeps a wildly out-of-bounds range within a buffered window", () => {
  const clamped = kline.clampLogicalRange({ from: -500, to: 900 }, 96, { rightOffset: 8 });
  assert.deepEqual(clamped, { from: -12, to: 107 });
});

test("clampLogicalRange preserves requested width when it already fits", () => {
  const clamped = kline.clampLogicalRange({ from: 10, to: 40 }, 96, { rightOffset: 8 });
  assert.equal(clamped.to - clamped.from, 30);
});

test("clampLogicalRange enforces a minimum visible width", () => {
  const tiny = kline.clampLogicalRange({ from: 50, to: 50.1 }, 96, { bufferBars: 12, minVisibleBars: 6 });
  assert.ok(tiny.to - tiny.from >= 6 - 1e-9);
});

test("clampLogicalRange returns null for invalid inputs", () => {
  assert.equal(kline.clampLogicalRange(null, 96, {}), null);
  assert.equal(kline.clampLogicalRange({ from: 0, to: 10 }, 0, {}), null);
  assert.equal(kline.clampLogicalRange({ from: NaN, to: 10 }, 96, {}), null);
});

test("nearestTime snaps a timestamp to the closest candle time", () => {
  const candles = [{ time: 100 }, { time: 160 }, { time: 220 }];
  assert.equal(kline.nearestTime(candles, 150), 160);
  assert.equal(kline.nearestTime(candles, 95), 100);
  assert.equal(kline.nearestTime([], 100), null);
});

test("toEpochSeconds accepts ISO strings, epoch seconds, and epoch milliseconds", () => {
  assert.equal(kline.toEpochSeconds("2026-01-01T00:00:00Z"), Math.floor(Date.parse("2026-01-01T00:00:00Z") / 1000));
  assert.equal(kline.toEpochSeconds(1735689600), 1735689600);
  assert.equal(kline.toEpochSeconds(1735689600000), 1735689600);
  assert.equal(kline.toEpochSeconds(null), null);
});

test("normalizeQualityFlags accepts arrays and delimited strings", () => {
  assert.deepEqual(kline.normalizeQualityFlags(["a", " b ", ""]), ["a", "b"]);
  assert.deepEqual(kline.normalizeQualityFlags("a, b|c"), ["a", "b", "c"]);
  assert.deepEqual(kline.normalizeQualityFlags(null), []);
});
