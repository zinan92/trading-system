(function() {
	// \0rolldown/runtime.js
	var __defProp = Object.defineProperty;
	var __exportAll = (all, no_symbols) => {
		let target = {};
		for (var name in all) __defProp(target, name, {
			get: all[name],
			enumerable: true
		});
		if (!no_symbols) __defProp(target, Symbol.toStringTag, { value: "Module" });
		return target;
	};
	// src/gridmind/format.ts
	var num = (v) => v === null || v === void 0 || v === "" ? "--" : Number.isFinite(Number(v)) ? Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "--";
	var finiteNumber = (value) => value !== null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
	var pnlClass = (v) => Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "";
	function timestampDate(timestamp) {
		if (timestamp == null || timestamp === "") return /* @__PURE__ */ new Date("");
		const text = String(timestamp).trim(), numeric = Number(text);
		if (text !== "" && Number.isFinite(numeric) && /^\d+(?:\.\d+)?$/.test(text)) {
			const magnitude = Math.abs(numeric);
			const milliseconds = magnitude >= 0x16345785d8a0000 ? numeric / 1e6 : magnitude >= 0x5af3107a4000 ? numeric / 1e3 : magnitude >= 1e11 ? numeric : numeric * 1e3;
			return new Date(milliseconds);
		}
		return new Date(timestamp);
	}
	function beijingDateTime(timestamp) {
		const date = timestampDate(timestamp);
		if (Number.isNaN(date.getTime())) return "--";
		const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
			year: "numeric",
			month: "2-digit",
			day: "2-digit",
			hour: "2-digit",
			minute: "2-digit",
			hourCycle: "h23",
			timeZone: "Asia/Shanghai"
		}).formatToParts(date).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
		return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
	}
	// src/gridmind/read.ts
	var read_exports = /* @__PURE__ */ __exportAll({
		ACCESS_REAUTH_KEY: () => ACCESS_REAUTH_KEY,
		OPERATOR_FAILURE_COPY: () => OPERATOR_FAILURE_COPY,
		acceptMarketSnapshot: () => acceptMarketSnapshot,
		apiFailure: () => apiFailure,
		carrySupervisorAudit: () => carrySupervisorAudit,
		createApi: () => createApi,
		dailyMarketKey: () => dailyMarketKey,
		dashboardMarketBarsUrl: () => dashboardMarketBarsUrl,
		dashboardMarketBinding: () => dashboardMarketBinding,
		displayTradingPair: () => displayTradingPair,
		emptyOrderLifecycle: () => emptyOrderLifecycle,
		headerMarket: () => headerMarket,
		isFresh: () => isFresh,
		isPublicObserver: () => isPublicObserver,
		isTrustedHeaderMarket: () => isTrustedHeaderMarket,
		marketFailure: () => marketFailure,
		marketTodayChangePct: () => marketTodayChangePct,
		marketUtcDayKey: () => marketUtcDayKey,
		mergeMarketBars: () => mergeMarketBars,
		nextHeaderPriceDirection: () => nextHeaderPriceDirection,
		operatorFailure: () => operatorFailure,
		readModelCycleId: () => readModelCycleId,
		readSession: () => readSession,
		reconcileOrderLifecycle: () => reconcileOrderLifecycle,
		reconcileReadModelMarket: () => reconcileReadModelMarket,
		retainLastTrustedMarket: () => retainLastTrustedMarket,
		sameVenueMarket: () => sameVenueMarket,
		startReadModelPolling: () => startReadModelPolling,
		statusText: () => statusText,
		tradingFailure: () => tradingFailure,
		usableDailyMarket: () => usableDailyMarket
	});
	function apiFailure(message, status = 0, code = "", details = {}) {
		const error = Error(message);
		error.status = status;
		error.code = code;
		error.details = details;
		return error;
	}
	var ACCESS_REAUTH_KEY = "goldbot-access-reauth";
	/** Off localhost the page sits behind Cloudflare Access; on it, it is the operator's own machine. */
	var isPublicObserver = (hostname) => ![
		"127.0.0.1",
		"localhost",
		"::1"
	].includes(hostname);
	/** The page's one way to call the dashboard. Headers and credentials are part of the control wire (ADR 0008). */
	function createApi(publicObserver) {
		return async function api(url, opts = {}) {
			const headers = new Headers(opts.headers || {});
			headers.set("Accept", "application/json");
			headers.set("X-Requested-With", "XMLHttpRequest");
			let response;
			try {
				response = await fetch(url, {
					...opts,
					headers,
					credentials: "same-origin"
				});
			} catch (error) {
				if (error?.name === "AbortError") throw error;
				throw apiFailure("浏览器未收到 Dashboard 响应", 0, "dashboard_response_unconfirmed", {
					cause: String(error?.message || error),
					url
				});
			}
			const contentType = (response.headers.get("content-type") || "").toLowerCase();
			const text = await response.text();
			const accessLoginHtml = publicObserver && contentType.includes("text/html") && (/Cloudflare Access/i.test(text) || /<title>\s*Sign in/i.test(text));
			if (response.status === 401 || accessLoginHtml) {
				if (publicObserver && sessionStorage.getItem("goldbot-access-reauth") !== "1") {
					sessionStorage.setItem(ACCESS_REAUTH_KEY, "1");
					setTimeout(() => location.reload(), 80);
				}
				throw apiFailure("登录会话已过期，正在重新登录……", 401, "access_session_expired");
			}
			let body = {};
			if (text) {
				if (!contentType.includes("application/json")) {
					const cloudflare = /cloudflare|error\s*1033|tunnel error/i.test(text);
					throw apiFailure(cloudflare ? "Cloudflare 隧道未能连接 Dashboard。" : `服务暂时返回了网页而不是数据（HTTP ${response.status}）。`, response.status, cloudflare ? "cloudflare_tunnel_unavailable" : "unexpected_content_type", {
						content_type: contentType,
						response_text: text.slice(0, 500),
						url
					});
				}
				try {
					body = JSON.parse(text);
				} catch (_error) {
					throw apiFailure("服务返回的数据格式异常，请刷新后重试。", response.status, "invalid_json");
				}
			}
			if (response.ok && publicObserver) sessionStorage.removeItem(ACCESS_REAUTH_KEY);
			if (!response.ok) {
				const detail = body?.detail && typeof body.detail === "object" ? body.detail : {};
				const code = body.error || detail.error || detail.reject_reason || "http_error";
				throw apiFailure(body.message || detail.detail || body.error || `请求失败（HTTP ${response.status}）`, response.status, code, {
					body,
					url
				});
			}
			return body;
		};
	}
	var OPERATOR_FAILURE_COPY = Object.freeze({
		dashboard_response_unconfirmed: {
			title: "Dashboard 响应未收到",
			reason: "浏览器未收到 Dashboard 的响应；后台是否已收到请求、Paper 是否已改变，当前无法确认。",
			action: "不要重复点击控制按钮；系统会核对真实运行状态。读取请求可刷新后重试。"
		},
		cloudflare_tunnel_unavailable: {
			title: "Cloudflare 隧道不可达",
			reason: "浏览器已到达 Cloudflare，但隧道未能连接本地 Dashboard（HTTP 530/1033）。",
			action: "检查或等待 Cloudflare Tunnel 恢复后刷新；这不会改变本地 Paper 委托。"
		},
		binance_upstream_unavailable: {
			title: "Binance USD-M 行情上游不可用",
			reason: "Dashboard 无法取得执行场所的可信实时行情。",
			action: "已保留最后可信 K 线时仅供查看；等待行情恢复后重新预览或启动，禁止新开仓。"
		},
		execution_grid_rolled_back: {
			title: "网格未被完整接受，已安全回滚",
			reason: "执行层没有接受完整网格；可能短暂收到部分回执，但控制面会撤回并拒绝启动。",
			action: "刷新当前委托与持仓；确认均为零后修改参数或检查执行器，再重新启动。"
		},
		dashboard_service_unavailable: {
			title: "Dashboard 服务暂不可用",
			reason: "请求已到达 Dashboard 边界，但服务暂时无法完成处理。",
			action: "稍后刷新；若持续发生，重启本地 Dashboard 服务后重新读取。"
		},
		execution_reconciliation_drift: {
			title: "账户对账未通过",
			reason: "账户余额、盈亏或交易身份与账本不一致，系统无法确认执行状态。",
			action: "先完成账户对账，再重新生成启动预览。"
		},
		paper_execution_tick_unavailable: {
			title: "Paper 执行心跳不可用",
			reason: "执行调度器没有提供足够新的完整 tick，不能安全推进或启动策略。",
			action: "检查 datafeed、生命周期和账本阶段；恢复完整 tick 后再启动。"
		},
		prepare_start_response_lost: {
			title: "启动规格回执丢失",
			reason: "后台可能已生成只读启动规格，但浏览器没有收到回执；此步骤不会下单。",
			action: "不要重复提交控制请求；重新点击启动机器人以读取一份新的冻结规格。"
		},
		prepare_start_outcome_unknown: {
			title: "启动规格结果仍未确认",
			reason: "浏览器无法确认后台是否已生成启动规格；此步骤不会下单。",
			action: "不要重复提交控制请求；刷新运行状态后重新点击启动机器人。"
		},
		start_not_applied: {
			title: "机器人尚未启动",
			reason: "运行状态已确认仍为停止，未创建新的 Paper 委托。",
			action: "检查行情与启动条件后重新生成预览。"
		},
		control_outcome_unknown: {
			title: "操作结果仍未确认",
			reason: "浏览器响应中断后，控制审计回执和运行状态仍不足以确认操作结果。",
			action: "不要重复点击；刷新运行状态，确认控制回执或请先安全停止。"
		},
		control_request_rejected: {
			title: "操作未完成",
			reason: "请求未通过当前控制条件。",
			action: "按页面提示修改参数或恢复依赖后重试；未确认成功前不会假定订单已创建。"
		}
	});
	function operatorFailure(kind, reason = "") {
		const copy = OPERATOR_FAILURE_COPY[kind] || OPERATOR_FAILURE_COPY.control_request_rejected;
		const renderedReason = reason || copy.reason;
		return {
			kind,
			title: copy.title,
			reason: renderedReason,
			action: copy.action,
			text: `${copy.title}：${renderedReason} 下一步：${copy.action}`
		};
	}
	/** Names the failure for Park: which link in the chain broke and what to do next. */
	function tradingFailure(error) {
		const raw = String(error?.message || error || "未知错误"), code = String(error?.code || ""), status = Number(error?.status) || 0;
		const details = error?.details || {}, serialized = `${raw} ${code} ${JSON.stringify(details)}`.toLowerCase();
		if (code === "dashboard_response_unconfirmed" || code === "request_not_reached_backend" || status === 0) return operatorFailure("dashboard_response_unconfirmed");
		if (OPERATOR_FAILURE_COPY[code]) return operatorFailure(code);
		if (status === 530 || code === "cloudflare_tunnel_unavailable" || /cloudflare.*(1033|tunnel)|error\s*1033/.test(serialized)) return operatorFailure("cloudflare_tunnel_unavailable");
		if (/binance|binance_usdm|fapi\.binance|upstream_error|datafeed/.test(serialized)) return operatorFailure("binance_upstream_unavailable");
		if (/complete grid|complete_grid|did not accept the complete grid|partial.*accept/.test(serialized)) return operatorFailure("execution_grid_rolled_back");
		if (status >= 500) return operatorFailure("dashboard_service_unavailable", `请求已到达 Dashboard 边界，但服务返回 HTTP ${status}。`);
		return operatorFailure("control_request_rejected");
	}
	/** A market snapshot that came back untrusted, as an error tradingFailure can name. */
	function marketFailure(incoming) {
		const raw = [
			...incoming?.access_issues || [],
			incoming?.operational_error,
			incoming?.error,
			incoming?.message
		].filter(Boolean).map(String).join(" / ") || String(incoming?.status || "unknown");
		return apiFailure(raw, Number((raw.match(/HTTP\s*(\d{3})/i) || [])[1]) || 0, /binance|binance_usdm|fapi\.binance|upstream_error|datafeed/i.test(raw) ? "binance_upstream_error" : "market_unavailable", { market: incoming });
	}
	/** Only a snapshot the datafeed marked trusted counts. Anything else blocks new entries. */
	function isFresh(m) {
		return m?.trusted === true;
	}
	/** The top bar quotes a price only from a trusted 1m snapshot. */
	function isTrustedHeaderMarket(m) {
		return isFresh(m) && m?.timeframe === "1m";
	}
	function statusText(m) {
		return isFresh(m) ? "实时·可信" : m?.retained_last_trusted ? "已阻断·保留最后可信K线" : "异常·停止新开仓";
	}
	function displayTradingPair(market) {
		const perp = String(market?.instrument_id || "").toUpperCase().match(/^(.+)-USD-PERP$/);
		if (perp) return `${perp[1]} / USD`;
		const raw = String(market?.provider_symbol || "").toUpperCase().replace(/\s+/g, "");
		if (!raw) return "--";
		if (raw.includes("/")) return raw.split("/").filter(Boolean).join(" / ");
		if (raw.includes("-")) {
			const parts = raw.split("-").filter((part) => part && !["SWAP", "PERP"].includes(part));
			if (parts.length >= 2) return parts.join(" / ");
		}
		for (const quote of [
			"USDT",
			"USDC",
			"USD",
			"BTC",
			"ETH"
		]) if (raw.endsWith(quote) && raw.length > quote.length) return `${raw.slice(0, -quote.length)} / ${quote}`;
		return raw;
	}
	function sameVenueMarket(left, right) {
		const leftSymbol = String(left?.provider_symbol || "").toUpperCase(), rightSymbol = String(right?.provider_symbol || "").toUpperCase();
		const leftProvider = String(left?.provider || ""), rightProvider = String(right?.provider || "");
		return Boolean(leftSymbol && rightSymbol && leftSymbol === rightSymbol && leftProvider && rightProvider && leftProvider === rightProvider);
	}
	/** The control card's selection, when it points the chart at Hyperliquid Testnet instead of the Paper feed. */
	function dashboardMarketBinding(selection) {
		if (selection?.venue_profile_id !== "hyperliquid.testnet" || !selection?.instrument_id) return null;
		const instrumentId = String(selection.instrument_id);
		return {
			venueProfileId: "hyperliquid.testnet",
			instrumentId,
			symbol: instrumentId.replace(/-USD-PERP$/i, "")
		};
	}
	function dashboardMarketBarsUrl(binding, { symbol, timeframe, limit, end = null }) {
		if (!binding) {
			const suffix = end ? `&end=${encodeURIComponent(end)}` : "";
			return `/api/dualtrack/market/bars?symbol=${encodeURIComponent(symbol || "GOLD")}&timeframe=${encodeURIComponent(timeframe)}&limit=${encodeURIComponent(limit)}${suffix}`;
		}
		const params = new URLSearchParams({
			venue_profile_id: binding.venueProfileId,
			instrument_id: binding.instrumentId,
			timeframe: String(timeframe),
			limit: String(limit)
		});
		if (end) params.set("end", String(end));
		return `/api/dashboard-control/market-bars?${params.toString()}`;
	}
	function marketUtcDayKey(market) {
		const date = timestampDate(market?.latest_timestamp ?? market?.bars?.at(-1)?.timestamp);
		return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(0, 10);
	}
	/** Today's change from the venue's own daily open, or null unless both snapshots are trusted, same venue, same UTC day. */
	function marketTodayChangePct(market, dailyMarket) {
		if (!isTrustedHeaderMarket(market) || !isFresh(dailyMarket) || dailyMarket?.timeframe !== "1d" || !sameVenueMarket(market, dailyMarket) || !marketUtcDayKey(market) || marketUtcDayKey(market) !== marketUtcDayKey(dailyMarket)) return null;
		const current = finiteNumber(market.latest_close ?? market.bars?.at(-1)?.close), open = finiteNumber((dailyMarket.bars || []).at(-1)?.open);
		return current !== null && open !== null && open > 0 ? (current / open - 1) * 100 : null;
	}
	function nextHeaderPriceDirection(previous, current, direction = "") {
		if (previous === null || current === null || current === previous) return direction;
		return current > previous ? "up" : "down";
	}
	/** The top bar's price, tick colour and today's change. Only a trusted 1m price moves the memo. */
	function headerMarket(market, memo, dailyMarket) {
		const price = finiteNumber(market?.latest_close ?? market?.bars?.at(-1)?.close), fresh = isTrustedHeaderMarket(market);
		const identity = `${String(market?.provider || "").toLowerCase()}|${String(market?.provider_symbol || "").toUpperCase()}`;
		let next = { ...memo };
		if (identity !== next.identity) next = {
			identity,
			lastPrice: null,
			direction: ""
		};
		if (fresh && price !== null) next = {
			...next,
			direction: nextHeaderPriceDirection(next.lastPrice, price, next.direction),
			lastPrice: price
		};
		const change = marketTodayChangePct(market, dailyMarket);
		return {
			memo: next,
			view: {
				pair: displayTradingPair(market),
				price: price === null ? "--" : num(price),
				priceClass: fresh && next.direction ? `tick-${next.direction}` : "",
				priceTitle: fresh ? `最新 1m 行情 · ${beijingDateTime(market.latest_timestamp)}` : "最新 1m 行情未通过可信门控",
				change: change === null ? "--" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`,
				changeClass: `header-change ${change === null ? "" : pnlClass(change)}`,
				changeTitle: change === null ? "交易所当日涨跌幅暂不可用" : "交易所当日开盘价至今"
			}
		};
	}
	/** The key one daily snapshot answers for, or "" when the base market cannot ask for one. */
	function dailyMarketKey(baseMarket) {
		if (!isTrustedHeaderMarket(baseMarket)) return "";
		const day = marketUtcDayKey(baseMarket);
		return day ? `${baseMarket.symbol || "GOLD"}|${baseMarket.provider || ""}|${baseMarket.provider_symbol || ""}|${day}` : "";
	}
	/** A daily snapshot is kept only when it is trusted, daily, and from the same venue as the base market. */
	function usableDailyMarket(baseMarket, daily) {
		return isFresh(daily) && daily.timeframe === "1d" && sameVenueMarket(baseMarket, daily);
	}
	function mergeMarketBars(existing, incoming) {
		if (!existing?.bars?.length) return incoming;
		if (!incoming?.bars?.length) return existing;
		if (String(existing.symbol) !== String(incoming.symbol) || existing.timeframe !== incoming.timeframe) return incoming;
		const rows = /* @__PURE__ */ new Map();
		[...existing.bars, ...incoming.bars].forEach((row) => {
			if (row?.timestamp) rows.set(row.timestamp, row);
		});
		const bars = [...rows.values()].sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
		return {
			...existing,
			...incoming,
			bars,
			bar_count: bars.length,
			oldest_timestamp: bars[0]?.timestamp || "",
			latest_timestamp: bars.at(-1)?.timestamp || incoming.latest_timestamp
		};
	}
	/** A refresh failed: keep the last trusted candles to look at, and mark them untrusted so nothing opens on them. */
	function retainLastTrustedMarket(existing, failure) {
		if (!existing?.bars?.length || existing.is_synthetic === true) return failure || existing;
		const accessIssues = Array.isArray(failure?.access_issues) ? failure.access_issues.filter(Boolean).map(String) : [];
		const issue = failure?.operational_error || failure?.error || failure?.message || accessIssues.join(" / ") || "最新行情刷新未通过可信门控";
		return {
			...existing,
			status: "error",
			fresh: false,
			trusted: false,
			retained_last_trusted: true,
			last_trusted_timestamp: existing.latest_timestamp,
			operational_error: issue,
			access_issues: accessIssues.length ? accessIssues : [issue],
			failed_refresh_status: failure?.status || "error"
		};
	}
	function acceptMarketSnapshot(incoming, existing) {
		if (isFresh(incoming) && incoming?.bars?.length) return {
			...mergeMarketBars(existing, incoming),
			retained_last_trusted: false,
			last_trusted_timestamp: null,
			operational_error: "",
			failed_refresh_status: null
		};
		return retainLastTrustedMarket(existing, incoming);
	}
	/** The read model's own market, folded into the chart's: a trusted chart market is never replaced by it. */
	function reconcileReadModelMarket(existing, incoming) {
		if (!existing || isFresh(existing)) return existing || incoming;
		return existing.retained_last_trusted ? acceptMarketSnapshot(incoming, existing) : incoming;
	}
	function readModelCycleId(data) {
		return String(data?.execution?.cycle_id || data?.contract?.source_identities?.cycle_id || data?.cycle?.cycle_id || "");
	}
	var emptyOrderLifecycle = () => ({
		cycleId: null,
		byId: /* @__PURE__ */ new Map(),
		anonymous: [],
		currentIds: /* @__PURE__ */ new Set()
	});
	/** Each order's furthest state this cycle: a poll that arrives late never walks an order back. */
	function reconcileOrderLifecycle(lifecycle, data) {
		const cycleId = readModelCycleId(data), incoming = Array.isArray(data?.execution?.orders) ? data.execution.orders : [];
		const next = !cycleId || lifecycle.cycleId !== cycleId ? {
			cycleId: cycleId || null,
			byId: /* @__PURE__ */ new Map(),
			anonymous: [],
			currentIds: /* @__PURE__ */ new Set()
		} : {
			...lifecycle,
			byId: new Map(lifecycle.byId)
		};
		next.currentIds = new Set(incoming.map((order) => String(order?.order_id || "").trim()).filter(Boolean));
		if (!cycleId) {
			next.anonymous = incoming.slice();
			return next;
		}
		next.anonymous = incoming.filter((order) => !String(order?.order_id || "").trim());
		for (const order of incoming) {
			const orderId = String(order?.order_id || "").trim();
			if (!orderId) continue;
			const previous = next.byId.get(orderId), rank = Number(order?.state_rank), previousRank = Number(previous?.state_rank);
			const revision = Number(order?.state_revision), previousRevision = Number(previous?.state_revision);
			const hasRevision = Number.isInteger(revision) && revision > 0, hasPreviousRevision = Number.isInteger(previousRevision) && previousRevision > 0;
			const advances = !previous || !Number.isFinite(previousRank) || Number.isFinite(rank) && rank > previousRank;
			const advancesRevision = previous && Number.isFinite(rank) && rank === previousRank && order?.state !== previous?.state && hasRevision && hasPreviousRevision && revision > previousRevision;
			const refreshesSameState = previous && Number.isFinite(rank) && rank === previousRank && order?.state === previous?.state && (!hasPreviousRevision || hasRevision && revision >= previousRevision);
			if (advances || advancesRevision || refreshesSameState) next.byId.set(orderId, { ...order });
		}
		return next;
	}
	/** A new read model keeps the on-demand supervisor history only while it is for the same cycle. */
	function carrySupervisorAudit(audit, data) {
		const cycleId = readModelCycleId(data);
		const next = audit.cycle && audit.cycle !== cycleId ? {
			cycle: "",
			audit: null,
			request: null
		} : { ...audit };
		if (next.audit && next.cycle === cycleId && data?.runtime) data.runtime.supervisor = next.audit;
		return next;
	}
	/** Who is reading: the local operator always may control; off localhost the session says. */
	function readSession(api, publicObserver) {
		return publicObserver ? api("/api/auth/session") : Promise.resolve({
			authenticated: true,
			can_control: true,
			email: null,
			session_duration: "local"
		});
	}
	/** Reads now, then every 10 s off localhost (5 s on it), and again when the tab comes back; never while a control call is busy or the tab is hidden. */
	function startReadModelPolling(load, { publicObserver, isBusy }) {
		load();
		window.setInterval(() => {
			if (!isBusy() && !document.hidden) load();
		}, publicObserver ? 1e4 : 5e3);
		document.addEventListener("visibilitychange", () => {
			if (!document.hidden && !isBusy()) load();
		});
	}
	// src/gridmind/read-entry.ts
	window.gmRead = read_exports;
})();
