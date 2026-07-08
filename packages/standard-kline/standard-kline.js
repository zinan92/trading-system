/*!
 * standard-kline
 *
 * Provider-agnostic OHLCV candlestick chart wrapper around TradingView
 * Lightweight Charts. Adapts any OHLCV-shaped payload into chart data,
 * flags synthetic/placeholder data, and drives zoom/pan/fit through a
 * clamped logical-range path that stays correct even when the caller
 * (or a user) requests an out-of-bounds range.
 *
 * Browser global: window.StandardKline
 * Node export: require("standard-kline")
 *
 * Peer dependency: lightweight-charts (loaded as window.LightweightCharts
 * in the browser, e.g. via the vendored standalone build).
 */
(function(root, factory){
  const api = factory(root || {});
  if(typeof module === "object" && module.exports) module.exports = api;
  if(root) root.StandardKline = api;
})(typeof window !== "undefined" ? window : globalThis, function(root){
  "use strict";

  const COLORS = {
    up:"#35d07f",
    down:"#ef5f5f",
    text:"#9aa3ad",
    faint:"#5d666f",
    grid:"rgba(255,255,255,.08)",
    line:"rgba(255,255,255,.16)",
    gold:"#d8aa3f",
    red:"#ef5f5f",
    amber:"#e8a33d",
    panel:"rgba(10,11,12,.82)",
    ema:["#d8aa3f", "#7aa2ff", "#b894ff"],
    macd:"rgba(53,208,127,.5)",
    macdNegative:"rgba(239,95,95,.5)",
    macdLine:"#9aa3ad",
    macdSignal:"#e8eaed",
  };

  function normalizeQualityFlags(value){
    if(Array.isArray(value)) return value.map(item => String(item || "").trim()).filter(Boolean);
    if(value == null || value === "") return [];
    return String(value).split(/[,\s|]+/).map(item => item.trim()).filter(Boolean);
  }

  function toEpochSeconds(value){
    if(value == null || value === "") return null;
    if(typeof value === "number" && Number.isFinite(value)){
      return Math.floor(value > 1e12 ? value / 1000 : value);
    }
    const text = String(value).trim();
    if(/^\d{13}$/.test(text)) return Math.floor(Number(text) / 1000);
    if(/^\d{10}$/.test(text)) return Number(text);
    const parsed = Date.parse(text);
    if(Number.isFinite(parsed)) return Math.floor(parsed / 1000);
    return null;
  }

  function numberOrNull(value){
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function clampNumber(value, min, max){
    return Math.min(max, Math.max(min, value));
  }

  function clampLogicalRange(range, barCount, options){
    if(!range || !Number.isFinite(Number(barCount)) || Number(barCount) <= 0) return null;
    let from = Number(range.from);
    let to = Number(range.to);
    if(!Number.isFinite(from) || !Number.isFinite(to)) return null;
    if(to < from) [from, to] = [to, from];

    const count = Math.max(1, Math.floor(Number(barCount)));
    const configuredBuffer = numberOrNull(options?.bufferBars);
    const buffer = Math.max(0, Math.floor(configuredBuffer ?? Math.max(8, Math.ceil(count * .12))));
    const rightOffset = Math.max(0, numberOrNull(options?.rightOffset) ?? 0);
    const minFrom = -buffer;
    const maxTo = Math.max(minFrom + 1, count - 1 + Math.max(buffer, rightOffset));
    const maxWidth = Math.max(1, maxTo - minFrom);
    const minVisibleBars = clampNumber(numberOrNull(options?.minVisibleBars) ?? 6, 1, maxWidth);

    let width = to - from;
    if(!Number.isFinite(width) || width <= 0) width = minVisibleBars;
    width = clampNumber(width, minVisibleBars, maxWidth);

    let center = (from + to) / 2;
    const minCenter = minFrom + width / 2;
    const maxCenter = maxTo - width / 2;
    if(minCenter <= maxCenter) center = clampNumber(center, minCenter, maxCenter);
    else center = (minFrom + maxTo) / 2;

    return {from:center - width / 2, to:center + width / 2};
  }

  function isSyntheticMeta(meta, syntheticFlags){
    if(!meta) return true;
    const flags = normalizeQualityFlags(meta.quality_flags).map(flag => flag.toLowerCase());
    const provider = String(meta.provider || "").toLowerCase();
    const sourceMode = String(meta.source_mode || "").toLowerCase();
    const extra = new Set(normalizeQualityFlags(syntheticFlags).map(flag => flag.toLowerCase()));
    return meta.is_synthetic === true
      || provider.includes("synthetic")
      || sourceMode.includes("synthetic")
      || flags.some(flag => extra.has(flag));
  }

  function adaptBarPayload(payload, options){
    const rows = Array.isArray(payload?.bars) ? payload.bars : [];
    const syntheticFlags = options?.syntheticFlags;
    const sourceFlags = normalizeQualityFlags(payload?.quality_flags);
    const byTime = new Map();

    rows.forEach((row, index) => {
      const time = toEpochSeconds(row.timestamp ?? row.time);
      const open = numberOrNull(row.open);
      const high = numberOrNull(row.high);
      const low = numberOrNull(row.low);
      const close = numberOrNull(row.close);
      if(time == null || open == null || high == null || low == null || close == null) return;
      const volume = Math.max(0, numberOrNull(row.volume) ?? 0);
      const qualityFlags = normalizeQualityFlags(row.quality_flags);
      byTime.set(time, {
        time,
        sourceIndex:index,
        candle:{time, open, high, low, close},
        volume:{time, value:volume, color:close >= open ? "rgba(53,208,127,.42)" : "rgba(239,95,95,.38)"},
        meta:{
          symbol:row.symbol ?? payload?.symbol ?? "",
          timeframe:row.timeframe ?? payload?.timeframe ?? "",
          timestamp:row.timestamp ?? "",
          provider:row.provider ?? payload?.provider ?? "",
          source_mode:payload?.source_mode ?? "",
          quality_flags:qualityFlags,
          is_synthetic:row.is_synthetic === true || payload?.is_synthetic === true,
        },
      });
    });

    const sorted = Array.from(byTime.values()).sort((a,b) => a.time - b.time);
    const barMeta = sorted.map(item => item.meta);
    const mergedFlags = Array.from(new Set(sourceFlags.concat(barMeta.flatMap(item => item.quality_flags))));
    const meta = {
      schema_version:payload?.schema_version || "",
      status:payload?.status || "",
      symbol:payload?.symbol || barMeta.at(-1)?.symbol || "",
      timeframe:payload?.timeframe || barMeta.at(-1)?.timeframe || "",
      provider:payload?.provider || barMeta.at(-1)?.provider || "",
      source_mode:payload?.source_mode || "",
      quality_flags:mergedFlags,
      is_synthetic:payload?.is_synthetic === true,
      requested:payload?.requested || {},
      bar_count:sorted.length,
      latest_timestamp:payload?.latest_timestamp || barMeta.at(-1)?.timestamp || "",
      fresh:payload?.fresh,
      access_issues:Array.isArray(payload?.access_issues) ? payload.access_issues.slice() : [],
    };
    meta.is_synthetic = isSyntheticMeta(meta, syntheticFlags);
    return {
      candles:sorted.map(item => item.candle),
      volumes:sorted.map(item => item.volume),
      barMeta,
      meta,
    };
  }

  function nearestTime(candles, timestamp){
    const target = toEpochSeconds(timestamp);
    if(target == null || !candles?.length) return null;
    let best = candles[0].time;
    let bestDistance = Infinity;
    candles.forEach(candle => {
      const distance = Math.abs(Number(candle.time) - target);
      if(distance < bestDistance){
        best = candle.time;
        bestDistance = distance;
      }
    });
    return best;
  }

  function formatPrice(value, digits){
    return Number(value || 0).toLocaleString("en-US", {minimumFractionDigits:digits, maximumFractionDigits:digits});
  }

  function formatChartTime(value, timeZone, includeDate, locale){
    const zone = timeZone || "UTC";
    const lang = locale || "en-US";
    let date = null;
    if(typeof value === "number"){
      date = new Date(value * 1000);
    }else if(value && typeof value === "object" && value.year && value.month && value.day){
      date = new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)));
    }else{
      const parsed = toEpochSeconds(value);
      if(parsed != null) date = new Date(parsed * 1000);
    }
    if(!date || Number.isNaN(date.getTime())) return "";
    const options = includeDate
      ? {timeZone:zone, month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}
      : {timeZone:zone, hour:"2-digit", minute:"2-digit", hour12:false};
    return new Intl.DateTimeFormat(lang, options).format(date);
  }

  function normalizePeriod(value, fallback){
    const parsed = Math.floor(Number(value ?? fallback));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : Math.floor(Number(fallback));
  }

  function computeEmaValues(points, period){
    const safePeriod = normalizePeriod(period, 1);
    const multiplier = 2 / (safePeriod + 1);
    let previous = null;
    return (points || []).map(point => {
      const value = numberOrNull(point.value);
      if(value == null) return null;
      previous = previous == null ? value : value * multiplier + previous * (1 - multiplier);
      return {time:point.time, value:previous};
    }).filter(Boolean);
  }

  function computeEma(candles, period){
    const points = (candles || []).map(candle => ({time:candle.time, value:candle.close}));
    return computeEmaValues(points, period);
  }

  function computeMacd(candles, options){
    const fast = normalizePeriod(options?.fast, 12);
    const slow = normalizePeriod(options?.slow, 26);
    const signalPeriod = normalizePeriod(options?.signal, 9);
    const fastEma = computeEma(candles, fast);
    const slowEma = computeEma(candles, slow);
    const slowByTime = new Map(slowEma.map(point => [point.time, point.value]));
    const macd = fastEma.map(point => {
      const slowValue = slowByTime.get(point.time);
      if(!Number.isFinite(slowValue)) return null;
      return {time:point.time, value:point.value - slowValue};
    }).filter(Boolean);
    const signal = computeEmaValues(macd, signalPeriod);
    const signalByTime = new Map(signal.map(point => [point.time, point.value]));
    const histogram = macd.map(point => {
      const signalValue = signalByTime.get(point.time);
      return {time:point.time, value:point.value - (Number.isFinite(signalValue) ? signalValue : 0)};
    });
    return {macd, signal, histogram};
  }

  function lineStyleValue(style){
    const lwc = root.LightweightCharts || {};
    if(style === "solid") return lwc.LineStyle?.Solid ?? 0;
    if(style === "dotted") return lwc.LineStyle?.Dotted ?? 1;
    return lwc.LineStyle?.Dashed ?? 2;
  }

  function injectStyles(){
    if(!root.document || root.document.getElementById("standard-kline-styles")) return;
    const style = root.document.createElement("style");
    style.id = "standard-kline-styles";
    style.textContent = `
.standard-kline-root{position:relative;width:100%;height:100%;min-height:320px;display:grid;grid-template-rows:auto minmax(0,1fr);background:transparent;overflow:hidden}
.standard-kline-root.compact{min-height:220px}
.standard-kline-toolbar{display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;min-height:30px;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.08);font:11px/1.2 var(--mono,"SFMono-Regular",ui-monospace,monospace);color:${COLORS.faint};background:rgba(255,255,255,.018)}
.standard-kline-toolbar button{border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:${COLORS.text};border-radius:5px;padding:3px 8px;cursor:pointer;font:inherit}
.standard-kline-toolbar button:hover{border-color:rgba(216,170,63,.45);color:${COLORS.gold}}
.standard-kline-crosshair{flex:0 0 auto;color:${COLORS.text};min-width:92px}
.standard-kline-source{margin-left:auto;min-width:0;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:58%;color:${COLORS.faint};text-align:right}
.standard-kline-canvas{position:relative;min-width:0;min-height:0;height:100%}
.standard-kline-overlay{position:absolute;inset:32px 14px 14px 14px;display:none;place-items:center;text-align:center;pointer-events:none;z-index:4}
.standard-kline-overlay.is-visible{display:grid}
.standard-kline-message{max-width:min(520px,94%);border:1px solid rgba(255,255,255,.15);background:${COLORS.panel};padding:14px 18px;color:${COLORS.text};font:12px/1.45 var(--mono,"SFMono-Regular",ui-monospace,monospace)}
.standard-kline-message b{display:block;font-size:18px;color:${COLORS.red};margin-bottom:4px;letter-spacing:0}
.standard-kline-message span{color:${COLORS.red}}
.standard-kline-empty .standard-kline-message b{color:${COLORS.amber}}
`;
    root.document.head.appendChild(style);
  }

  class StandardKlineChart {
    constructor(container, options){
      if(!root.document) throw new Error("StandardKlineChart requires a browser document");
      this.container = typeof container === "string" ? root.document.querySelector(container) : container;
      if(!this.container) throw new Error("StandardKlineChart container not found");
      this.options = {...options};
      this.chart = null;
      this.candleSeries = null;
      this.volumeSeries = null;
      this.markerApi = null;
      this.emaSeries = [];
      this.macdPane = null;
      this.macdSeries = null;
      this.macdSignalSeries = null;
      this.macdHistogramSeries = null;
      this.priceLines = [];
      this.current = adaptBarPayload(null);
      this.resizeObserver = null;
      this.loading = false;
      this.destroyed = false;
      this._timeScale = null;
      this._nativeSetVisibleLogicalRange = null;
      this._nativeGetVisibleLogicalRange = null;
      injectStyles();
      this._buildDom();
      this._initChart();
    }

    _buildDom(){
      this.container.innerHTML = "";
      this.rootEl = root.document.createElement("div");
      this.rootEl.className = `standard-kline-root${this.options.compact ? " compact" : ""}`;
      this.rootEl.dataset.standardKline = "true";
      this.toolbarEl = root.document.createElement("div");
      this.toolbarEl.className = "standard-kline-toolbar";
      this.toolbarEl.innerHTML = `<button type="button" data-action="zoom-in" title="Zoom in">+</button><button type="button" data-action="zoom-out" title="Zoom out">-</button><button type="button" data-action="pan-left" title="Pan left">&lt;</button><button type="button" data-action="pan-right" title="Pan right">&gt;</button><button type="button" data-action="fit" title="Fit">fit</button><span class="standard-kline-crosshair" data-crosshair-time></span><span class="standard-kline-source" data-source></span>`;
      this.chartEl = root.document.createElement("div");
      this.chartEl.className = "standard-kline-canvas";
      this.overlayEl = root.document.createElement("div");
      this.overlayEl.className = "standard-kline-overlay";
      this.overlayEl.dataset.standardKlineOverlay = "true";
      this.overlayEl.innerHTML = `<div class="standard-kline-message"><b></b><span></span></div>`;
      this.rootEl.appendChild(this.toolbarEl);
      this.rootEl.appendChild(this.chartEl);
      this.rootEl.appendChild(this.overlayEl);
      this.container.appendChild(this.rootEl);
      this.toolbarEl.addEventListener("click", event => {
        const action = event.target?.closest?.("button[data-action]")?.dataset?.action;
        if(!action) return;
        if(action === "zoom-in") this.zoom(0.72);
        if(action === "zoom-out") this.zoom(1.38);
        if(action === "pan-left") this.pan(-12);
        if(action === "pan-right") this.pan(12);
        if(action === "fit") this.fit();
      });
    }

    _initChart(){
      const lwc = root.LightweightCharts;
      if(!lwc?.createChart){
        this._setOverlay("empty", "CHART LIBRARY MISSING", "window.LightweightCharts is not loaded (peer dependency).");
        return;
      }
      const size = this._size();
      const timeZone = this.options.timeZone || "UTC";
      const locale = this.options.locale || "en-US";
      this.chart = lwc.createChart(this.chartEl, {
        width:size.width,
        height:size.height,
        layout:{background:{type:"solid", color:"transparent"}, textColor:this.options.textColor || COLORS.text, fontSize:11},
        grid:{vertLines:{color:this.options.gridColor || "rgba(255,255,255,.045)"}, horzLines:{color:this.options.gridColor || COLORS.grid}},
        crosshair:{mode:lwc.CrosshairMode?.Normal ?? 1},
        rightPriceScale:{borderVisible:false, minimumWidth:1, scaleMargins:{top:.08,bottom:this.options.showVolume === false ? .10 : .28}},
        timeScale:{borderVisible:true, timeVisible:true, secondsVisible:false, rightOffset:8, barSpacing:this.options.compact ? 5 : 7, minBarSpacing:.5, rightBarStaysOnScroll:true, tickMarkFormatter:time => formatChartTime(time, timeZone, false, locale)},
        handleScroll:{mouseWheel:true, pressedMouseMove:true, horzTouchDrag:true, vertTouchDrag:true},
        handleScale:{axisPressedMouseMove:true, mouseWheel:true, pinch:true},
        localization:{priceFormatter:price => formatPrice(price,2), timeFormatter:time => formatChartTime(time, timeZone, true, locale)},
      });
      this.chart.subscribeCrosshairMove?.(param => this._setCrosshairTime(param?.time));
      this._patchTimeScale();
      this.candleSeries = this.chart.addSeries(lwc.CandlestickSeries, {
        upColor:COLORS.up,
        downColor:COLORS.down,
        borderUpColor:COLORS.up,
        borderDownColor:COLORS.down,
        wickUpColor:COLORS.up,
        wickDownColor:COLORS.down,
        priceLineVisible:false,
        lastValueVisible:true,
        priceFormat:{type:"price", precision:2, minMove:.01},
      });
      this.candleSeries.priceScale().applyOptions({scaleMargins:{top:.08,bottom:this.options.showVolume === false ? .10 : .28}});
      if(this.options.showVolume !== false){
        this.volumeSeries = this.chart.addSeries(lwc.HistogramSeries, {
          priceFormat:{type:"volume"},
          priceScaleId:"",
          priceLineVisible:false,
          lastValueVisible:false,
        });
        this.volumeSeries.priceScale().applyOptions({scaleMargins:{top:.78,bottom:.02}});
      }
      if(lwc.createSeriesMarkers) this.markerApi = lwc.createSeriesMarkers(this.candleSeries, []);
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(this.container);
      this.resize();
    }

    _patchTimeScale(){
      if(!this.chart?.timeScale) return;
      const nativeTimeScale = this.chart.timeScale.bind(this.chart);
      const timeScale = nativeTimeScale();
      if(!timeScale) return;
      this._timeScale = timeScale;
      this._nativeSetVisibleLogicalRange = timeScale.setVisibleLogicalRange?.bind(timeScale) || null;
      this._nativeGetVisibleLogicalRange = timeScale.getVisibleLogicalRange?.bind(timeScale) || null;
      const owner = this;
      if(this._nativeSetVisibleLogicalRange){
        timeScale.setVisibleLogicalRange = function(range){
          return owner._setVisibleLogicalRange(range);
        };
      }
      if(timeScale.fitContent){
        timeScale.fitContent = function(){
          return owner.fit();
        };
      }
    }

    _size(){
      const rect = this.container.getBoundingClientRect();
      const width = Math.max(260, Math.floor(rect.width || this.options.width || 640));
      const height = Math.max(this.options.compact ? 220 : 320, Math.floor(rect.height || this.options.height || 380));
      return {width, height};
    }

    resize(){
      if(!this.chart || this.destroyed) return;
      const size = this._size();
      this.chart.applyOptions({width:size.width, height:size.height});
    }

    setLoading(loading, message){
      this.loading = Boolean(loading);
      if(this.loading) this._setOverlay("loading", "LOADING KLINE", message || "Waiting for OHLCV bars.");
      else this._refreshOverlay();
    }

    /**
     * Adapt a raw OHLCV payload and render it in one call.
     * `overlays.priceLines` / `overlays.markers` are generic arrays this
     * chart draws as-is — build them from your own domain model (trade
     * plans, fills, alerts, ...) before calling this.
     */
    setPayload(payload, overlays){
      const adapted = adaptBarPayload(payload, {syntheticFlags:this.options.syntheticFlags});
      return this.setAdaptedData(adapted, overlays);
    }

    setAdaptedData(adapted, overlays){
      this.current = adapted || adaptBarPayload(null);
      const candles = this.current.candles || [];
      if(this.candleSeries) this.candleSeries.setData(candles);
      if(this.volumeSeries) this.volumeSeries.setData(this.current.volumes || []);
      this._setPriceLines(overlays?.priceLines || []);
      this._setMarkers(overlays?.markers || []);
      this._setIndicators(overlays?.indicators || null);
      this._setSourceText(this.current.meta);
      this._refreshOverlay();
      if(candles.length && overlays?.fit !== false) this.fit();
      return this.current;
    }

    _setMarkers(markers){
      const sorted = (markers || []).slice().sort((a,b) => Number(a.time) - Number(b.time));
      if(this.markerApi?.setMarkers) this.markerApi.setMarkers(sorted);
      else if(this.candleSeries?.setMarkers) this.candleSeries.setMarkers(sorted);
    }

    _setPriceLines(lines){
      if(this.candleSeries?.removePriceLine){
        this.priceLines.forEach(line => this.candleSeries.removePriceLine(line));
      }
      this.priceLines = [];
      (lines || []).forEach(line => {
        const price = numberOrNull(line.price);
        if(price == null || !this.candleSeries?.createPriceLine) return;
        this.priceLines.push(this.candleSeries.createPriceLine({
          price,
          color:line.color || COLORS.gold,
          lineWidth:line.lineWidth || 1,
          lineStyle:lineStyleValue(line.lineStyle),
          axisLabelVisible:true,
          title:line.title || "",
        }));
      });
    }

    _removeSeries(series){
      if(!series || !this.chart?.removeSeries) return;
      try{ this.chart.removeSeries(series); }catch(_error){}
    }

    _clearIndicators(){
      this.emaSeries.forEach(series => this._removeSeries(series));
      this.emaSeries = [];
      [this.macdHistogramSeries, this.macdSeries, this.macdSignalSeries].forEach(series => this._removeSeries(series));
      this.macdHistogramSeries = null;
      this.macdSeries = null;
      this.macdSignalSeries = null;
      if(this.macdPane && this.chart?.removePane){
        try{ this.chart.removePane(this.macdPane.paneIndex()); }catch(_error){}
      }
      this.macdPane = null;
    }

    _setIndicators(indicators){
      this._clearIndicators();
      if(!this.chart || !this.current?.candles?.length || !indicators) return;
      const lwc = root.LightweightCharts || {};
      const candles = this.current.candles || [];
      const emaConfigs = Array.isArray(indicators.ema) ? indicators.ema.slice(0, 3) : [];
      emaConfigs.forEach((config, index) => {
        if(!lwc.LineSeries) return;
        const period = normalizePeriod(config?.period, 20);
        const series = this.chart.addSeries(lwc.LineSeries, {
          color:config?.color || COLORS.ema[index] || COLORS.gold,
          lineWidth:config?.lineWidth || 1,
          priceLineVisible:false,
          lastValueVisible:false,
        });
        series.setData(computeEma(candles, period));
        this.emaSeries.push(series);
      });
      if(indicators.macd && lwc.HistogramSeries && lwc.LineSeries && this.chart.addPane){
        const macdConfig = indicators.macd || {};
        this.macdPane = this.chart.addPane();
        this.macdPane.setHeight?.(Math.max(110, Number(this.options.macdPaneHeight || 128)));
        this.macdHistogramSeries = this.macdPane.addSeries(lwc.HistogramSeries, {
          priceFormat:{type:"price", precision:3, minMove:.001},
          priceLineVisible:false,
          lastValueVisible:false,
        });
        this.macdSeries = this.macdPane.addSeries(lwc.LineSeries, {
          color:macdConfig.macdColor || COLORS.macdLine,
          lineWidth:1,
          priceLineVisible:false,
          lastValueVisible:false,
        });
        this.macdSignalSeries = this.macdPane.addSeries(lwc.LineSeries, {
          color:macdConfig.signalColor || COLORS.macdSignal,
          lineWidth:1,
          priceLineVisible:false,
          lastValueVisible:false,
        });
        const macd = computeMacd(candles, macdConfig);
        this.macdHistogramSeries.setData(macd.histogram.map(point => ({
          time:point.time,
          value:point.value,
          color:point.value >= 0 ? COLORS.macd : COLORS.macdNegative,
        })));
        this.macdSeries.setData(macd.macd);
        this.macdSignalSeries.setData(macd.signal);
      }
    }

    _setSourceText(meta){
      const source = this.toolbarEl.querySelector("[data-source]");
      if(!source) return;
      const mode = meta?.source_mode || "unknown";
      const provider = meta?.provider || "unknown";
      const count = meta?.bar_count || this.current.candles.length || 0;
      const flags = normalizeQualityFlags(meta?.quality_flags);
      const fullText = `${meta?.symbol || "--"} ${meta?.timeframe || ""} · ${mode} · ${provider} · ${count} bars${flags.length ? " · " + flags.join(",") : ""}`;
      let customText = "";
      if(typeof this.options.sourceFormatter === "function"){
        try{ customText = this.options.sourceFormatter(meta, this.current) || ""; }catch(_error){}
      }
      source.textContent = customText || `${meta?.symbol || "--"} ${meta?.timeframe || ""} · ${String(mode).replace(/_/g, " ")} · ${count} bars`;
      source.title = fullText;
    }

    _setCrosshairTime(time){
      const target = this.toolbarEl.querySelector("[data-crosshair-time]");
      if(!target) return;
      if(time == null){
        target.textContent = "";
        return;
      }
      target.textContent = formatChartTime(time, this.options.timeZone || "UTC", true, this.options.locale || "en-US");
    }

    _refreshOverlay(){
      if(this.loading) return;
      const candles = this.current.candles || [];
      if(!candles.length){
        this._setOverlay("empty", "NO KLINE DATA", "No valid OHLCV bars were provided to the standard adapter.");
        return;
      }
      if(this.current.meta?.is_synthetic){
        this._setOverlay("synthetic", "SIMULATED DATA / NOT REAL PRICE", "Display-only seed or synthetic bars. Do not use for trading signal or manual order decisions.");
        return;
      }
      this._hideOverlay();
    }

    _setOverlay(kind, title, detail){
      this.overlayEl.classList.add("is-visible", `standard-kline-${kind}`);
      this.overlayEl.dataset.state = kind;
      this.overlayEl.querySelector("b").textContent = title;
      this.overlayEl.querySelector("span").textContent = detail;
    }

    _hideOverlay(){
      this.overlayEl.classList.remove("is-visible", "standard-kline-loading", "standard-kline-empty", "standard-kline-synthetic");
      this.overlayEl.dataset.state = "ready";
    }

    _barCount(){
      return this.current?.candles?.length || 0;
    }

    _rightOffset(){
      const offset = numberOrNull(this._timeScale?.options?.()?.rightOffset);
      return offset == null ? 8 : offset;
    }

    _clampLogicalRange(range){
      return clampLogicalRange(range, this._barCount(), {
        bufferBars:this.options.logicalRangeBufferBars,
        minVisibleBars:this.options.minVisibleBars,
        rightOffset:this._rightOffset(),
      });
    }

    _readLogicalRange(){
      return this._nativeGetVisibleLogicalRange?.() || this._timeScale?.getVisibleLogicalRange?.() || null;
    }

    _setVisibleLogicalRange(range){
      const target = this._clampLogicalRange(range);
      if(!target || !this._timeScale) return null;
      const width = Math.max(.001, target.to - target.from);
      const scaleWidth = Math.max(1, Number(this._timeScale.width?.() || this._size().width || 1));
      const scaleOptions = this._timeScale.options?.() || {};
      const minSpacing = Math.max(.1, numberOrNull(scaleOptions.minBarSpacing) ?? .5);
      const configuredMax = numberOrNull(scaleOptions.maxBarSpacing);
      const maxSpacing = configuredMax && configuredMax > 0 ? configuredMax : Math.max(40, numberOrNull(this.options.maxBarSpacing) ?? 80);
      const barSpacing = clampNumber(scaleWidth / width, minSpacing, maxSpacing);
      this._timeScale.applyOptions?.({barSpacing});
      this._timeScale.scrollToPosition?.(target.to - (this._barCount() - 1), false);
      return target;
    }

    fit(){
      if(!this.chart) return null;
      const count = this._barCount();
      if(!count) return null;
      return this._setVisibleLogicalRange({from:-1, to:count - 1 + this._rightOffset()});
    }

    zoom(factor){
      const scale = this.chart?.timeScale?.();
      const range = scale?.getVisibleLogicalRange?.();
      const parsedFactor = numberOrNull(factor);
      if(parsedFactor == null || parsedFactor <= 0) return null;
      if(!range) return;
      const safeFactor = clampNumber(parsedFactor, .2, 5);
      const center = (range.from + range.to) / 2;
      const half = Math.max(3, (range.to - range.from) * safeFactor / 2);
      return scale.setVisibleLogicalRange({from:center - half, to:center + half});
    }

    pan(bars){
      const scale = this.chart?.timeScale?.();
      const range = scale?.getVisibleLogicalRange?.();
      const step = numberOrNull(bars);
      if(step == null) return null;
      if(!range) return;
      return scale.setVisibleLogicalRange({from:range.from + step, to:range.to + step});
    }

    destroy(){
      this.destroyed = true;
      this.resizeObserver?.disconnect?.();
      this.chart?.remove?.();
      this.container.innerHTML = "";
    }
  }

  return {
    StandardKlineChart,
    adaptBarPayload,
    clampLogicalRange,
    computeEma,
    computeMacd,
    isSyntheticMeta,
    nearestTime,
    normalizeQualityFlags,
    toEpochSeconds,
  };
});
