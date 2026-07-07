<div align="center">

# standard-kline

**把任意 OHLCV 数据源渲染成可信 K 线图的标准前端组件。**

[![JavaScript](https://img.shields.io/badge/javascript-umd%20%2B%20commonjs-f7df1e.svg)](standard-kline.js)
[![Lightweight Charts](https://img.shields.io/badge/lightweight--charts-%5E5.2.0-2962ff.svg)](https://github.com/tradingview/lightweight-charts)
[![Tests](https://img.shields.io/badge/tests-node%20--test-2ea44f.svg)](standard-kline.test.js)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

```text
in  OHLCV bars + provider/source metadata + optional overlay arrays
out candlestick chart + volume histogram + price lines + markers + synthetic-data watermark

fail missing LightweightCharts → visible "chart library missing" overlay
fail invalid/missing OHLC rows  → drop bad rows instead of crashing
fail empty payload             → visible "no kline data" overlay
fail synthetic/demo data       → visible "not real price" watermark
fail extreme zoom/pan range    → clamp to data window + buffer
```

`standard-kline` 是一个小型、无构建依赖的 K 线图包。它不关心你的数据来自 Tiger、Binance、券商 API、回测文件，还是未来别的数据源；只要输入能整理成统一 OHLCV bars，就能稳定渲染、缩放、平移、复位，并把真实数据和 synthetic/demo 数据明确区分开。

## 适用场景

- 交易 dashboard 需要复用同一套 K 线组件。
- 多个行情源已经在后端标准化成 OHLCV bars。
- 前端需要显示 provider、source mode、quality flags，避免把 placeholder 当真实价格。
- 应用有自己的业务概念，例如 trade plan、fills、alerts，但不想把这些概念写进图表包。

## 安装

### Browser UMD

```html
<script src="lightweight-charts.standalone.production.js"></script>
<script src="standard-kline.js"></script>

<div id="chart" style="height: 420px"></div>

<script>
  const chart = new StandardKline.StandardKlineChart(
    document.getElementById("chart"),
    { height: 420, showVolume: true }
  );
</script>
```

### Node / Bundler

```bash
npm install lightweight-charts
```

```js
const {
  StandardKlineChart,
  adaptBarPayload,
  nearestTime,
} = require("standard-kline");
```

`lightweight-charts` 是 peer dependency。浏览器直引时，请确保 `window.LightweightCharts` 在创建图表前已经加载。

## 输入契约

```js
{
  schema_version: "ohlcv-v1",
  status: "ready",
  source_mode: "rest_poll",
  symbol: "BTCUSD",
  timeframe: "1m",
  provider: "example_exchange",
  quality_flags: [],
  is_synthetic: false,
  bars: [
    {
      timestamp: "2026-01-01T00:00:00Z",
      open: 100,
      high: 102,
      low: 99,
      close: 101,
      volume: 5,
      provider: "example_exchange",
      quality_flags: [],
    },
  ],
}
```

每根 bar 只强制要求 `timestamp/open/high/low/close`。`timestamp` 支持 ISO UTC 字符串、epoch seconds、epoch milliseconds。坏行会被跳过，不会让整张图崩掉。

## 基本用法

```js
const chart = new StandardKline.StandardKlineChart("#chart", {
  height: 380,
  showVolume: true,
  syntheticFlags: ["demo_seed", "display_only"],
});

chart.setPayload(payload, {
  priceLines: [
    {
      price: 4200,
      title: "resistance",
      color: "#7aa2ff",
      lineStyle: "dashed",
      lineWidth: 1,
    },
  ],
  markers: [
    {
      time: 1783213260,
      position: "belowBar",
      color: "#7aa2ff",
      shape: "arrowUp",
      text: "entry 4180.0",
    },
  ],
  fit: true,
});

chart.zoom(0.72); // < 1 zoom in, > 1 zoom out
chart.pan(12);    // shift by logical bars
chart.fit();      // show the full safe data window
```

## Overlay 设计

这个包只认识通用 overlay：

- `priceLines`: `{ price, title, color, lineStyle, lineWidth }[]`
- `markers`: Lightweight Charts marker objects

它不会内置 trade plan、fill、human/machine track、strategy signal 等业务模型。调用方应该在自己的应用层把业务对象转成 `priceLines` 和 `markers`，再传给图表。

如果 marker 的时间戳不一定刚好落在 candle 上，可以先用 `nearestTime`：

```js
const markerTime = StandardKline.nearestTime(adapted.candles, fill.timestamp);
```

## Synthetic / Demo 数据

默认 synthetic 判定只看通用信号：

- `payload.is_synthetic === true`
- `provider` 包含 `"synthetic"`
- `source_mode` 包含 `"synthetic"`

如果你的系统用自己的 `quality_flags` 标记 demo/seed/display-only 数据，在构造图表或调用 adapter 时显式传入：

```js
const syntheticFlags = ["synthetic_seed", "display_only", "not_for_trading_signal"];

const chart = new StandardKline.StandardKlineChart("#chart", {
  syntheticFlags,
});

const adapted = StandardKline.adaptBarPayload(payload, { syntheticFlags });
```

被判定为 synthetic 的 payload 会显示明显水印，避免被误认为真实交易价格。

## API

### `new StandardKlineChart(container, options?)`

| Option | Default | Description |
|---|---:|---|
| `height` | `380` | 初始高度；实际会跟随容器 resize |
| `compact` | `false` | 更紧凑的默认高度和 bar spacing |
| `showVolume` | `true` | 是否显示 volume histogram |
| `textColor` | theme | chart text color |
| `gridColor` | theme | grid line color |
| `syntheticFlags` | `[]` | 额外 synthetic quality flags |
| `logicalRangeBufferBars` | `max(8, 12% bars)` | 允许 pan/zoom 超出数据的 buffer |
| `minVisibleBars` | `6` | 最小可见 bar 数 |
| `maxBarSpacing` | `80` | 最大水平 zoom |

### Chart methods

- `setPayload(payload, overlays?)`
- `setAdaptedData(adapted, overlays?)`
- `setLoading(loading, message?)`
- `zoom(factor)`
- `pan(bars)`
- `fit()`
- `destroy()`

### Pure helpers

- `adaptBarPayload(payload, options?)`
- `isSyntheticMeta(meta, syntheticFlags?)`
- `clampLogicalRange(range, barCount, options?)`
- `nearestTime(candles, timestamp)`
- `normalizeQualityFlags(value)`
- `toEpochSeconds(value)`

## 失败行为

| Failure | Behavior |
|---|---|
| `window.LightweightCharts` 未加载 | 显示 chart-library-missing overlay |
| payload 无有效 bars | 显示 no-kline-data overlay |
| 单行缺少 OHLC 或 timestamp | 跳过该行 |
| synthetic/demo 数据 | 显示 not-real-price watermark |
| `setVisibleLogicalRange({ from: -999, to: 999 })` | clamp 到数据窗口 + buffer |

## 测试

```bash
npm test
```

或直接运行：

```bash
node --test standard-kline.test.js
```

当前测试覆盖 adapter、timestamp conversion、synthetic detection、range clamp、nearest candle snapping。`StandardKlineChart` 需要真实 DOM 和 `lightweight-charts`，建议在接入应用里用 Playwright 做浏览器级验证。

## 发布状态

- 当前版本：`0.1.0`
- 模块格式：UMD + CommonJS
- peer dependency：`lightweight-charts ^5.2.0`
- license：MIT

## License

MIT. See [LICENSE](LICENSE).
