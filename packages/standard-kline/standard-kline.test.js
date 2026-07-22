const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const kline = require("./standard-kline.js");

const indicatorCandles = [
  { time: 1, close: 100 },
  { time: 2, close: 102 },
  { time: 3, close: 101 },
  { time: 4, close: 105 },
  { time: 5, close: 107 },
  { time: 6, close: 106 },
  { time: 7, close: 110 },
  { time: 8, close: 112 },
];

function roundedValues(points){
  return points.map(point => Number(point.value.toFixed(6)));
}

test("computeEma returns time-aligned EMA points for a hand-checked fixture", () => {
  assert.equal(typeof kline.computeEma, "function");

  const ema = kline.computeEma(indicatorCandles, 3);

  assert.deepEqual(ema.map(point => point.time), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(roundedValues(ema), [
    100,
    101,
    101,
    103,
    105,
    105.5,
    107.75,
    109.875,
  ]);
});

test("computeMacd returns time-aligned MACD, signal, and histogram points", () => {
  assert.equal(typeof kline.computeMacd, "function");

  const macd = kline.computeMacd(indicatorCandles, { fast: 3, slow: 6, signal: 3 });

  assert.deepEqual(macd.macd.map(point => point.time), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(roundedValues(macd.macd), [
    0,
    0.428571,
    0.306122,
    1.075802,
    1.625573,
    1.375409,
    1.946721,
    2.301229,
  ]);
  assert.deepEqual(roundedValues(macd.signal), [
    0,
    0.214286,
    0.260204,
    0.668003,
    1.146788,
    1.261098,
    1.60391,
    1.952569,
  ]);
  assert.deepEqual(roundedValues(macd.histogram), [
    0,
    0.214286,
    0.045918,
    0.407799,
    0.478785,
    0.114311,
    0.342811,
    0.34866,
  ]);
});

test("package files stay provider-agnostic and free of app business terms", () => {
  const text = ["standard-kline.js", "README.md", "package.json"]
    .map(file => fs.readFileSync(path.join(__dirname, file), "utf8"))
    .join("\n")
    .toLowerCase();

  [
    ["dual", "track"].join(""),
    ["hu", "man"].join(""),
    ["ma", "chine"].join(""),
    String.fromCharCode(30450),
    String.fromCharCode(20154, 36712),
    String.fromCharCode(26426, 22120),
    String.fromCharCode(20316, 25112),
  ].forEach(term => {
    assert.equal(text.includes(term), false, term);
  });
});

test("sanitizeElementIds rewrites repeated injected ids per chart instance", () => {
  assert.equal(typeof kline.sanitizeElementIds, "function");
  const nodes = ["tv-attr-logo", "a"].map(id => ({
    id,
    classes: [],
    classList: {
      add(value){
        this.owner.classes.push(value);
      },
    },
  }));
  nodes.forEach(node => {
    node.classList.owner = node;
  });
  const root = {
    querySelectorAll(selector){
      assert.equal(selector, "[id]");
      return nodes;
    },
  };

  const first = kline.sanitizeElementIds(root, "one");
  const second = kline.sanitizeElementIds(root, "one");

  assert.deepEqual(first.map(change => change.from), ["tv-attr-logo", "a"]);
  assert.deepEqual(first.map(change => change.to), ["tv-attr-logo-one-0", "a-one-1"]);
  assert.deepEqual(second, []);
  assert.deepEqual(nodes.map(node => node.id), ["tv-attr-logo-one-0", "a-one-1"]);
  assert.deepEqual(nodes.map(node => node.classes[0]), ["tv-attr-logo", "a"]);
});

test("chart wrapper exposes provider-agnostic price coordinate methods", () => {
  const source = fs.readFileSync(path.join(__dirname, "standard-kline.js"), "utf8");

  assert.match(source, /priceToY\(price\)/);
  assert.match(source, /yToPrice\(y\)/);
  assert.match(source, /priceToCoordinate/);
  assert.match(source, /coordinateToPrice/);
});

test("chart wrapper emits a provider-agnostic view change event", () => {
  const source = fs.readFileSync(path.join(__dirname, "standard-kline.js"), "utf8");

  assert.match(source, /standard-kline:viewchange/);
  assert.match(source, /subscribeVisibleLogicalRangeChange/);
  assert.match(source, /_emitViewChange\(reason, range\)/);
  assert.match(source, /_isNearLiveEdge\(range, barCount\)/);
  assert.match(source, /live-update/);
  assert.match(source, /getVisibleLogicalRange\(\)/);
  assert.match(source, /getVisibleOhlcRange\(fallbackBars\)/);
  assert.match(source, /restoreVisibleLogicalRange\(range, prependedBars\)/);
  assert.match(source, /setPriceLines\(lines\)/);
});

test("chart wrapper surfaces TradingView-style OHLC and scale controls", () => {
  const source = fs.readFileSync(path.join(__dirname, "standard-kline.js"), "utf8");

  assert.match(source, /data-ohlc/);
  assert.match(source, /O \$\{formatPrice\(candle\.open,2\)\} H \$\{formatPrice\(candle\.high,2\)\} L \$\{formatPrice\(candle\.low,2\)\} C \$\{formatPrice\(candle\.close,2\)\}/);
  assert.match(source, /data-crosshair-time-axis/);
  assert.match(source, /standard-kline-time-axis-label/);
  assert.doesNotMatch(source, /data-crosshair-time><\/span>/);
  assert.match(source, /subscribeCrosshairMove\?\.\(param => this\._setCrosshairTime\(param\)\)/);
  assert.match(source, /target\.style\.left/);
  assert.match(source, /target\.hidden = false/);
  assert.match(source, /@container \(max-width:720px\)\{\.standard-kline-source\{display:none\}\}/);
  assert.match(source, /data-action="auto-fit"/);
  assert.doesNotMatch(source, /data-action="toggle-log"/);
  assert.doesNotMatch(source, /toggleLogScale/);
  assert.doesNotMatch(source, /PriceScaleMode\?\.Logarithmic/);
});

test("chart wrapper keeps time axis visible and suppresses injected attribution text", () => {
  const source = fs.readFileSync(path.join(__dirname, "standard-kline.js"), "utf8");

  assert.match(source, /attributionLogo:false/);
  assert.match(source, /timeScale:\{visible:true, borderVisible:true, timeVisible:true/);
  assert.match(source, /NO LIVE KLINE DATA/);
  assert.match(source, /access_issues/);
});

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
