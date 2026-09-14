# Hyperliquid 平台与可复用工具深度研究

**研究日期：** 2026-08-21（Asia/Shanghai）
**研究仓库：** `standard-broker`
**状态：** read-only research；无 live/testnet 调用、无 credential、无交易代码、无部署、无云端状态修改。

## 0. 结论先行

Hyperliquid 值得接入，但它不只是“一个交易所 API”。它是一个以交易为核心的 L1：HyperCore 提供 fully-onchain perpetual/spot CLOB，HyperEVM 与 HyperCore 共用底层共识/状态，HIP-3 允许 builder 部署自己的 perpetual DEX。它有很强的平台化潜力，但“未来会不会最大”仍是市场判断，不是技术研究可以证明的结论。

对本项目的推荐是：

```text
trading-system
  └─ strategy / risk / Paper gate / trust & freshness / recording / Telegram
       └─ standard-broker
            ├─ canonical broker model
            ├─ six ports
            ├─ capability declaration + fail-closed policy
            ├─ receipts / provenance / fee model
            └─ Nautilus compatibility boundary
                 └─ NautilusTrader nautilus-hyperliquid
                      └─ official Hyperliquid REST + WS + EIP-712 wire
```

不要再造第二个 Hyperliquid signing、WebSocket、nonce、order lifecycle 和 reconciliation 栈。NautilusTrader v1.230.0 已经有 Rust-native、带 Python bindings 的 Hyperliquid adapter，直接走官方 REST/WebSocket，不依赖 CCXT 或外部 Hyperliquid SDK。

第一阶段范围：

- `broker_id=hyperliquid`；
- `environment=paper`；
- default validator-operated perpetuals；
- local fake/fixture/read-only serializer/conformance；
- 不接 mainnet、testnet、Spot、HIP-3、HIP-4、multi-DEX、MCP、LLM agent、bot runtime；
- 不改变策略，不改变现有 Paper/trust/freshness/reconciliation/Park/Telegram gates。

三个必须显式建模的 gap：

1. Hyperliquid 的 market order wire 实际上是带 slippage 的 aggressive IOC limit；不能假装是 native market。
2. `normalTpsl`、`positionTpsl` 和 partial-fill 后的 children 行为不同；不能默认保护单自动跟随所有仓位变化。
3. `modify` 是 cancel-replace；旧 OID、新 OID、同一 CLOID 的异步事件必须通过 lifecycle/reconciliation 处理。

---

## 1. 研究来源与证据等级

### 用户指定来源

- [`nktkas/hyperliquid`](https://github.com/nktkas/hyperliquid)
- [`nomeida/hyperliquid`](https://github.com/nomeida/hyperliquid)
- `/Users/wendy/park-hands/Clippings/Building a Hyperliquid AI Agent Trader From Scratch.md`
- [`Hummingbot Hyperliquid connector`](https://hummingbot.org/exchanges/hyperliquid/#spot-connector)
- [`Senpi-ai/senpi-skills`](https://github.com/Senpi-ai/senpi-skills)
- `/Users/wendy/park-hands/Clippings/This GPT-5.6 Trading Bot Is CRUSHING Hyperliquid 247 (so far).md`
- [`sanketagarwal/hyperliquid-trading-agent`](https://github.com/sanketagarwal/hyperliquid-trading-agent)
- [`Oak Research: HIP-3 analysis`](https://oakresearch.io/en/analyses/innovations/what-is-hyperliquid-hip-3-how-it-works-and-use-cases)

### 补充来源

- [Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs)
- [Official API overview](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api)
- [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
- [Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [WebSocket/subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [Order types](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)
- [TP/SL](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl)
- [Tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)
- [Nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)
- [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
- [Fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
- [Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
- [Liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)
- [Account abstraction](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)
- [HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)
- [Asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- [Testnet faucet](https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/testnet-faucet)
- [Builder codes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes)
- [Official Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- [Official Rust SDK](https://github.com/hyperliquid-dex/hyperliquid-rust-sdk)
- [Nautilus v1.230.0 Hyperliquid integration](https://github.com/nautechsystems/nautilus_trader/blob/v1.230.0/docs/integrations/hyperliquid.md)
- [Current Nautilus Hyperliquid integration](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/hyperliquid.md)
- [Nautilus adapter registry](https://github.com/nautechsystems/nautilus_trader/blob/develop/ADAPTERS.md)
- [CCXT Hyperliquid wiki](https://github.com/ccxt/ccxt/wiki/exchanges/hyperliquid)
- [Hummingbot connector architecture](https://hummingbot.org/connectors/connectors/architecture/)
- [Hummingbot MCP](https://github.com/hummingbot/mcp)
- [Hummingbot Condor](https://condor.hummingbot.org/introduction)
- [Dakkshin Hyperliquid MCP](https://github.com/Dakkshin/hyperliquid-mcp)
- [edkdev Hyperliquid MCP](https://github.com/edkdev/hyperliquid-mcp)

### 证据分级

- A：官方 docs/API/official GitHub，作为协议合同；
- A-：Nautilus v1.230.0 adapter/docs，作为已有引擎实现证据；
- B：Hummingbot、nktkas、nomeida、CCXT，作为成熟社区实现比较；
- C：Senpi、Condor、MCP，作为应用层架构参考；
- D：个人 bot/clipping/Oak，作为方向和风险观察，不作为接口 authority。

---

## 2. 平台事实与产品边界

### 2.1 HyperCore + HyperEVM

官方文档将 Hyperliquid 定义为交易优化的 L1。HyperCore 包含链上 perpetual/spot order book；订单、cancel、trade、liquidation 进入链上状态机。HyperEVM 让应用可以访问 HyperCore 金融 primitive，但文档仍标注渐进式 alpha/早期开放，不能把未来 CoreWriter/write system contract 当作当前能力。

对 adapter 的影响：

- 不能只写普通 HTTP client；必须处理签名者、nonce、链上 broker fact、WS 重连、snapshot 恢复和 reconciliation；
- Hyperliquid 是 CLOB，不是 AMM swap；
- 策略仍只能读 canonical ports，不能直接读取 raw Hyperliquid messages。

### 2.2 四类产品不要混成一个 instrument

| 产品 | 语义 | v1 |
|---|---|---|
| Default validator-operated perps | USDC collateral、线性 perpetual、主 DEX | **支持** |
| Native spot | Spot order book、token/pair metadata、spot balance | defer |
| HIP-3 builder perps | 每个 DEX 有自己的 collateral/margin/oracle/funding/liquidation/fee | defer；独立 boundary |
| HIP-4 outcomes | binary side tokens、settlement、特殊 asset ID/lifecycle | defer；独立 model |

HIP-3 使用统一 order API，但并不等价于普通 perpetual。不能把 `xyz:TSLA` 当作普通 `TSLA-PERP`；之后必须携带 `dex_scope`、collateral provenance、oracle/operator facts 和 deployer fee。

### 2.3 潜力与风险

潜力：交易基础设施与应用层共链；链上 CLOB；permissionless market deployment；官方 API/SDK 与成熟 Nautilus adapter 已存在。

风险：HIP-3 liquidity/oracle/collateral/fee fragmentation；API shape unversioned；agent signer 与 account address 容易混淆；nonce 是 per-signer；WS 会断；高杠杆/mark-price liquidation/ADL/partial fills/builder fee 远比 demo 复杂；tradfi-like HIP-3 还有监管与产品属性风险。

结论：技术上值得建 adapter；不对未来市占或 token 价值作保证。

---

## 3. 六个 canonical ports 的 Hyperliquid capability

### 3.1 MarketDataPort

| Canonical data | Hyperliquid source | 适配要求 |
|---|---|---|
| K-line | `candleSnapshot` / `candle` WS | 转为现有 standard K-line；raw candle 只能放 provenance |
| trades | `recentTrades` / `trades` WS | 保留时间、方向、px、size、hash/trade id |
| order book | `l2Book` snapshot/stream | 声明 full/aggregated depth；`nSigFigs`/`mantissa` 不可丢 |
| ticker/BBO/mid | `allMids`、`bbo`、asset context | mid、BBO、mark、oracle 分开 |
| mark/index/oracle | active asset context | mark 用于 margin/liquidation/TP-SL，不能用 last trade 替代 |
| freshness | venue time + local receive time + WS state | 进入现有 tick freshness/trust gate |
| provenance | endpoint/subscription/transport/adapter revision | 每条 canonical event 可回查 |
| execution venue | broker metadata | `broker_id=hyperliquid`；可记录 `hypercore:default` scope |

WebSocket 断线后，官方要求自动用户重连并补 missed data/snapshot。adapter 在 snapshot rebuild 完成前必须是 stale/unknown，不能继续发“新鲜”数据。历史 candle、recent trades、L2 levels 都有 endpoint 上限，必须记录 completeness。

### 3.2 InstrumentPort

官方 metadata 提供 asset index、`szDecimals`、`maxLeverage`、`onlyIsolated`/margin mode 和动态 asset context。

精度规则：

- price 最多 5 个 significant figures；
- perps price 最大小数位 `6 - szDecimals`；
- spot price 最大小数位 `8 - szDecimals`；
- size 按 `szDecimals`；
- signed wire 要去 trailing zeros。

canonical instrument 必须保存：

```text
contract_type = linear_perpetual
collateral_currency = USDC (default perp)
price_precision_rule
quantity_step = 10^(-szDecimals)
min_notional = verified venue fact, default perp = 10 quote units
min_quantity = derived from price + step when no fixed venue minimum exists
margin_mode
max_leverage
supported_order_types
instrument_revision / raw metadata hash
```

不能把 `min_quantity` 写成一个永恒固定数字：最小 notional 与当前价格共同决定可下的最小数量。

### 3.3 AccountPort

`clearinghouseState`、spot state、portfolio、user funding、non-funding ledger、user fills/user events 可覆盖：equity/NAV、balance、withdrawable、margin、exposure、entry/position/PnL、liquidation price、actual fee、funding、liquidation/backstop/ADL facts。

约束：

- query 使用实际 master/subaccount/vault address；agent/API wallet 只是 signer；
- standard/unified/portfolio margin 语义分开；v1 只允许 default perp + standard interpretation；
- unknown 保留 `None/unknown`，不把缺失字段变成 0；
- liquidation event 与 liquidation fee 事实分开。

### 3.4 OrderExecutionPort

| 能力 | 结论 | Hyperliquid nuance |
|---|---|---|
| submit/cancel/query/open orders/fills/positions | yes | single/batch，OID/CLOID |
| client order ID | yes | 128-bit hex `cloid` |
| idempotency | adapter-managed | persisted canonical id + CLOID + signer nonce |
| reconciliation | yes, required | order/fill/position/account 一起核对 |
| modify | yes, qualified | cancel-replace，旧/新 OID 可能乱序 |
| GTC/IOC/ALO | yes | ALO = post-only |
| FOK/GTD | no | fail closed |
| native market | no | aggressive IOC limit + slippage/BBO |

market intent 可以存在于 canonical model，但必须声明：`native_market=false`、`market_as_ioc_limit=true`、`requires_fresh_bbo=true`、`requires_slippage_policy=true`。没有 fresh quote/slippage/合法 precision 时拒绝，不猜价。

### 3.5 ProtectionOrderPort

| 能力 | 状态 | 语义 |
|---|---|---|
| reduce-only | yes | close-only；不减少仓位会拒绝/取消 |
| TP market/limit | yes | mark price trigger；market 有 slippage tolerance |
| SL market/limit | yes | mark price trigger；limit 可能触发但不成交 |
| OCO | yes, grouped | `normalTpsl`/`positionTpsl`，不是无条件泛化 endpoint |
| bracket parent-child | yes, grouped | parent/children 有特定 placement/cancel 规则 |
| position-level TP/SL | yes | `positionTpsl` |
| auto-follow quantity | yes, position mode | whole-position 默认会随仓位调整 |
| fixed quantity | yes, normal mode | explicit/parent size fixed |
| cancel-replace | yes, qualified | 必须走 lifecycle/reconciliation |
| partial-fill protection update | gap/qualified | parent partial-fill + ordinary cancel 可能取消 children，不能默认自动修复 |

官方文档明确：position form 的 TP/SL 默认 entire position；指定 size 后 fixed；parent order 的 TP/SL 等于 parent size；parent fully filled 才正常激活 children；普通 partial-fill cancel 可能取消 children；要保护已成交部分，必须另建/修复保护。

### 3.6 FeePort

| Fee fact | Source/contract |
|---|---|
| maker/taker | `userFees` schedule + actual fill |
| rebate | fill fee 可为负 |
| referral/staking discount | user-specific response，不能用默认费率替代 |
| builder fee | order builder + approval + actual fill provenance |
| funding | user funding stream/history，每小时、P2P，单独于交易费 |
| liquidation fee | 官方称无传统 clearance fee；记录 liquidation/maintenance loss，不虚构 fee |
| currency/time/source | fill `feeToken`、event time、raw source |
| schedule version | 官方没有稳定 semantic version；用 retrieval time + raw/docs hash |
| actual/estimated | pre-trade estimate 与 fill/ledger actual 分开 |

---

## 4. Order lifecycle 与 reconciliation

```text
canonical intent
  → capability/market trust/freshness/precision gates
  → persistent idempotency + client id
  → broker mapping + signer/nonce
  → REST/WS post
  → per-order response
  → WS order/fill events
  → canonical lifecycle
  → fee/funding/account/position reconciliation
  → recording/accounting receipt
```

Hyperliquid-specific rules：

1. HTTP `status=ok` 不是完整 lifecycle proof；batch 里还可能出现 resting/filled/waiting/error。
2. fill 必须以 `tid`、OID/CLOID、hash、time、fee、feeToken 做幂等；WS snapshot 和 REST reconciliation 不得重复记账。
3. IOC partial fill：已成交部分保留，未成交 remainder 取消；必须保存 original/filled/remaining quantity。
4. modify 是 cancel-replace：旧 OID、新 OID、同一 CLOID；可能先 old cancel、后 new accepted，也可能 fill 先到。需要 pending replacement state、buffer、promotion、reconciliation fallback。
5. signed request timeout 后状态是 `unknown/submitting`，不能盲目重试；应先按 CLOID/OID/open orders/fills/query 查证。
6. `expiresAfter` 是 broker stale guard；不支持的 user-signed actions 不得伪造该字段。
7. `cancelAll` 与 `scheduleCancel` 是不同 capability；后者是 dead-man switch。

Bracket 生命周期：

```text
parent + TP + SL
  → normalTpsl/positionTpsl
  → parent accepted
  → full fill: children active
  → partial fill: children may remain untriggered
  → ordinary parent cancel: children canceled
  → residual position: core must decide whether repair is allowed
  → one sibling fills: other sibling canceled
```

adapter 不得把没有 venue-native 保证的 repair 静默模拟成成功。

---

## 5. Credentials、nonce、rate limits、Paper boundary

### 5.1 API wallet

官方文档确认：master account 批准 agent/API wallet；agent 只负责签名；查询必须使用 master/subaccount address；使用 agent address 会读到空账户。nonce 按 signer 维持，多个 subaccount 共享一个 signer 会共享 nonce；官方建议每个 trading process 使用独立 API wallet，并做短窗口批处理。注销/过期 agent 的 nonce state 可能被 pruning，不应重复复用旧 agent address。

canonical descriptor 只允许：

```text
broker_id
environment
signer_kind
credential_env_names
account_address
vault_address
```

不允许 private key、seed、signed payload、完整 signature 出现在 log/receipt/issue/screenshot。

### 5.2 Rate limits

官方限制要进入 adapter policy：

- REST aggregate weight 1200/min；
- exchange batch weight 为 `1 + floor(batch_length/40)`；
- WebSocket max 10 connections、30 new connections/min、1000 subscriptions、10 unique users、2000 messages/min、100 inflight posts；
- address-based request limit 受累计交易量影响，cancel 有额外 allowance；
- batch 对 IP weight 可能算一次，对 address-based limit 按订单数算。

因此 rate limiter 不能只有一个本地 token bucket；需要同时处理 IP weight、address-based usage、WS inflight 和 retry/ambiguous outcome。无限 retry 是危险行为。

### 5.3 三个环境

| Environment | 当前状态 | 定义 |
|---|---|---|
| `paper` | **允许** | local fake/fixture/replay；network I/O=false；无 key |
| `testnet` | 后续批准 | 独立 network、asset IDs、agent approvals 和 account state；不是本地 paper |
| `mainnet` | **禁止** | live/真钱；另行授权 |

官方 testnet faucet 还要求同一地址先在 mainnet deposit 后才能 claim mock USDC，进一步说明 testnet 不是完全脱离外部状态的 unit-test substitute。

---

## 6. 现成工具评估

### 6.1 官方 Python SDK

[`hyperliquid-dex/hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) 是官方 organization 下的 MIT Python SDK，覆盖 `Info`、`Exchange`、WS、signing、order/modify/cancel、CLOID、TP/SL wire、grouping、account、fees、funding、HIP-3 metadata。官方 docs 建议复用 SDK，不要手写签名。

**决定：** 作为官方 wire/signing reference 和 fixture source；当前不作为 `standard-broker` runtime dependency，因为 Nautilus 已有 native adapter。

### 6.2 Nautilus `nautilus-hyperliquid`

Nautilus v1.230.0 已包含 Rust + Python bindings 的 Hyperliquid adapter，直接集成官方 REST/WS，不依赖外部 client library。覆盖 data client、execution client、factories、precision、trigger、reduce-only、post-only、batch、CLOID、account/fee/funding/liquidation/reconciliation 和 reconnect。

其文档也明确记录了限制：market 是 IOC-limit emulation；agent account address 必须正确；Python retry config 在该版本被接受但未完全消费；cancel-replace 的极窄 chained-modify race 仍由 reconciliation 兜底。

**决定：** 这是本项目第一实现来源。`standard-broker` 拥有 canonical contract/capability/provenance/fail-closed，Nautilus 拥有低层 signing/transport/lifecycle。

### 6.3 nktkas

[`nktkas/hyperliquid`](https://github.com/nktkas/hyperliquid) 是 MIT TypeScript SDK，覆盖 Node/Deno/Bun，含 typed schema、typed errors、HTTP/WS transport、wallet/multi-sig/vault/nonce/expiresAfter/builder 和大量官方 API methods。它明确提醒 Hyperliquid API unversioned，API shape 可能以 patch release 变化。

**决定：** 若未来选择 TypeScript，它是首选；当前 Python/Nautilus 基座不引入第二语言 runtime。

### 6.4 nomeida

[`nomeida/hyperliquid`](https://github.com/nomeida/hyperliquid) 是轻量 TypeScript REST/WS SDK，提供 trailing-zero、token bucket、reconnect、asset map refresh 和 API wallet account address 支持。package metadata 声明 MIT，但 repository metadata license 识别需要额外核验，维护规模弱于 nktkas。

**决定：** 对照参考，不是当前首选依赖。

### 6.5 CCXT

官方 Hyperliquid API 页面列出 CCXT；其 Hyperliquid wiki 覆盖 common order、trigger、reduce-only、CLOID、edit、funding、TWAP 和 WS。

CCXT 适合多交易所最低公分母，但本项目要保留 exact Hyperliquid trigger/OCO/positionTpsl、account-address/nonce、partial-fill protection、venue rejection 和 Nautilus event lifecycle。把这些都塞进 `params` 会增加复杂度并隐藏语义。

**决定：** 研究借鉴；不作为 `standard-broker` v1 runtime dependency。

### 6.6 Hummingbot

Hummingbot 有成熟的 Hyperliquid spot/perp connector、testnet、orderbook/user stream/in-flight order/client tracker/fee accounting。当前公开 connector capability 主要是 LIMIT/LIMIT_MAKER/MARKET，perp 是 one-way，没有完整暴露我们要求的 TP/SL/OCO/position-follow/partial-fill protection contract。

Hummingbot client 还默认附带 0-bps builder attribution；Condor AI Harness 使用 1 bps builder fee。Hummingbot MCP 连接 Hummingbot API，会额外引入 API server、bot lifecycle 和 credentials。

**决定：** 借鉴 connector/fee/accounting patterns；不引入整个 framework、MCP 或 Condor。

### 6.7 Senpi

[`Senpi-ai/senpi-skills`](https://github.com/Senpi-ai/senpi-skills) 的公开层是 skills + strategy templates；runtime/MCP/model 不是全在 repo 中。它的有价值模式是 scanner→deterministic risk/sizing→execution→DSL exits→telemetry；它明确声称 repo 本身不直接下单。

**决定：** 借鉴 risk/telemetry separation，不依赖其 managed runtime/MCP、token、strategy catalog 或 live wallet path。

### 6.8 Agent demo 与本地 clipping

[`sanketagarwal/hyperliquid-trading-agent`](https://github.com/sanketagarwal/hyperliquid-trading-agent) 有 code-level risk guards、Claude、agent wallet/master address 分离，但未审计、无明确 license，不能当 broker infrastructure。

两份 clipping 展示 skills/subagents/research/trade brief/recording/evolution loop，但也展示了直接 private key、live fire trade、高杠杆和个人短期收益曲线。只能作为应用层反面教材，不能复用到 `standard-broker`。

### 6.9 MCP

官方 API/official GitHub organization 没有官方 Hyperliquid MCP。社区有 Dakkshin、edkdev 等 MCP，普遍是“将 private key 放到 MCP env，再让 LLM 调 write tools”。它们可研究 response parsing、read/write tool separation 和 testnet toggle，但不能成为当前 broker layer。

**决定：** 当前不接任何 Hyperliquid MCP；未来最多作为 read-only research/diagnostic client，不能绕过 Telegram-only、Risk、freshness、reconciliation、Park 和 Supervisor gates。

---

## 7. 当前 `trading-system` 与 Nautilus 现状

当前 `trading-system` 已把 Nautilus 当作 Paper/replay execution engine：

- `services/dualtrack_nautilus_execution_adapter.py` 是 event-sourced、paper-only、durable normalized state；
- `services/execution_plugin_composition.py` 通过可信 registry 选择 `nautilus_paper`，并要求 attended approval、isolated runtime 和 cutover gate；
- `spikes/dualtrack_nautilus_shadow_replay.py` 明确不打开 venue client、不写 authoritative legacy ledger；
- 当前 Paper path 是 replay/fake semantics，不是 Hyperliquid live client。

当前 [`trading-system/docs/broker-port-spec.md`](/Users/wendy/work/trading-system/docs/broker-port-spec.md) 已规定：application 提交 normalized intent，concrete adapter 负责 symbol/payload/signature/network/lifecycle/reconciliation，capability absent 要在 network I/O 前失败，且官方 Nautilus execution client 可在 future milestone 满足该 port。

不要因为两者都叫 Nautilus，就把 Paper replay proof 当成 Hyperliquid live connectivity proof。两种证据必须分开。

---

## 8. `standard-broker` canonical domain model

### 8.1 Identity

所有对手方统一叫 `Broker`。`broker_id` 是主身份；`execution_venue_id` 只是 broker 内部 execution scope/provenance。

```text
BrokerIdentity
  broker_id
  environment = paper | testnet | mainnet
  account_scope = master | subaccount | vault
  account_address (public)
  signer_kind = none | wallet | api_agent

InstrumentIdentity
  broker_id
  canonical_symbol
  broker_symbol (mapping/provenance)
  contract_type
  quote_currency
  collateral_currency
  execution_scope
  metadata_revision/hash
```

核心模型不暴露 `coin`、`oid`、`cloid`、`normalTpsl`、`positionTpsl`、`dex` 作为策略/risk API；这些只允许出现在 adapter mapping/raw provenance/capability details。

### 8.2 MarketDataEnvelope

```text
schema_version
broker_id
execution_venue_id
instrument_id
data_kind = kline | trade | order_book | ticker | mark | index | funding
canonical_payload
provider_symbol/raw_symbol (provenance)
source_endpoint/subscription
venue_timestamp
received_timestamp
freshness_state
transport_state
raw_hash/adapter_revision
```

K-line payload 继续使用已有 standard K-line；不能创建 Hyperliquid 专属 K-line v2。data-feed 的 know-how 可以复用为 normalizer/freshness/fixture，不要求 data-feed 继续作为独立常驻服务。

### 8.3 Order/Protection/Fee

```text
OrderIntent
  order_id, broker_id, instrument_id, side, order_type
  quantity, limit_price?, trigger_price?, tif
  reduce_only, protection_plan_id?, idempotency_key, expires_at?

OrderReceipt
  order_id, broker_order_id?, client_order_id
  lifecycle_state, filled_qty, remaining_qty, average_price?
  fee_refs, ambiguity_state, last_provenance, reconciliation_state

ProtectionPlan
  protection_id, parent_order_id?, entry_scope = order | position
  tp/sl kind, trigger_reference, quantity_policy
  oco_policy, partial_fill_policy, cancel_replace_policy
  capability_evidence

FeeEvent
  fee_id, broker_id, order_id?, fill_id?, instrument_id?
  fee_kind, amount, currency, occurred_at
  source_kind, schedule_identity?, actual_or_estimated, raw_provenance
```

---

## 9. v1 capability matrix与 fail-closed

| Port/capability | v1 status | 规则 |
|---|---|---|
| MarketData K-line/trades/book/ticker | source supported | 只输出 canonical payload，保存 raw provenance |
| MarketData freshness/provenance | required | WS reconnect/snapshot 未完成即 stale/unknown |
| Instrument mapping/precision/step | supported | dynamic metadata；不硬编码 asset index |
| Instrument min quantity | derived | 由 price、step、min notional 推导，不伪造常量 |
| Instrument contract/margin/leverage | supported | default linear perp + metadata |
| Account equity/balance/margin/exposure/PnL | source supported | clearinghouse state，unknown 不归零 |
| Account fee/funding/liquidation facts | source supported | actual fee/funding/liquidation 分开 |
| Order submit/cancel/query/open/fills/positions | source supported | v1 只 fake transport |
| Order idempotency/client ID/reconciliation | required | persistent id + CLOID mapping + fill tid |
| Market order | qualified | aggressive IOC-limit + fresh BBO + slippage |
| FOK/GTD | gap | fail closed |
| reduce-only/TP-SL/OCO/bracket | qualified supported | exact group/trigger/quantity semantics |
| position-level auto-follow | supported in position mode | 不等于 normal fixed-size |
| partial-fill protection repair | gap/qualified | core 不默认自动成功 |
| maker/taker/rebate/funding | supported | schedule 与 actual 分开 |
| liquidation fee | no separate clearance fee | 记录 venue fact，不虚构 fee |
| fee schedule version | adapter-derived | retrieval time + raw/docs hash |
| testnet | deferred | 后续独立 credential/environment gate |
| mainnet | forbidden now | 不构造、不配置、不调用 |

核心必须在 transport 前拒绝：unsupported order/protection、stale/missing instrument mapping、unknown account mode/collateral、无 fresh BBO 的 market、ambiguous response 的盲重试、estimated fee 被当 actual、Paper path 出现 testnet/mainnet URL/key、策略/risk/Telegram 请求出现 raw Hyperliquid primitive。

---

## 10. Park 需要拍板的 A/B decisions

### Decision A：wire ownership

**A1：** standard-broker 自己实现 Python HTTP/WS/signing；repo 自包含，但会复制 Nautilus hard parts，产生第二套 signing/lifecycle/reconciliation 漂移。
**A2（推荐）：** standard-broker 拥有 canonical boundary/capability/provenance，低层使用 pinned Nautilus adapter；需要 version/commit compatibility contract 和 conformance fixtures。

### Decision B：v1 product scope

**B1：** 一次支持 Spot + default perps + HIP-3 + HIP-4；表面完整，但会把不同 collateral/margin/settlement/asset ID/lifecycle 一次压进核心。
**B2（推荐）：** v1 只支持 default validator-operated perps，Spot/HIP-3/HIP-4 以后以独立 capability/issue 接入。

### 另外三个建议默认值

- **Paper：** local fake/fixture only；testnet 是后续外部 integration，不算当前 Paper proof；
- **Market：** `native_market=false`、`market_as_ioc_limit=true`，必须 fresh BBO/slippage；
- **Builder attribution：** Paper 不发送；外部环境必须显式 approval、fee policy 和 provenance。

---

## 11. Stories：一 Issue = 一 branch = 一 PR

### SB-001 — Canonical broker domain and capability contract

**Outcome:** 六个 ports、broker identity、environment、capability、provenance、fail-closed contract 完成，core 不含 Hyperliquid primitive。
**验收标准：** public ports 可导入；model 不含 provider-required fields；capability frozen/validated；unsupported 在 transport 前报错；descriptor 不含 secret；paper/testnet/mainnet 不混用。
**In scope:** domain、capability、errors、provenance、paper boundary。
**Out of scope:** network client、Nautilus integration、strategy/risk/cloud/TG。
**禁区:** live/testnet keys、network I/O、CCXT/Hummingbot/Senpi dependency、削弱现有 gates。

### SB-002 — Default-perps instrument and market-data mapping

**Outcome:** 用 official/Nautilus fixtures 映射 default perps 与 canonical K-line/trade/book/ticker。
**验收标准：** asset index 不硬编码；precision/step/min notional/leverage/margin 可复验；K-line 与现有 contract 一致；book depth/freshness/provenance 明确；stale/reconnect fixture fail closed；Spot/HIP-3/HIP-4 明确 defer。
**In scope:** read-only fixtures、normalizers、metadata revision。
**Out of scope:** live/testnet WS、indicators、data-feed service redesign。
**禁区:** 第二套 K-line、raw message 进入策略、真实 API call。

### SB-003 — Nautilus Hyperliquid compatibility boundary

**Outcome:** standard-broker 使用 Nautilus adapter 作为可替换 wire implementation，不复制 signing/wire/lifecycle。
**验收标准：** version/commit 可审计；canonical↔Nautilus mapping 不泄漏 primitive；Paper 不构造 live/testnet client；capability 与实际 adapter 一致；version mismatch fail closed；raw receipt 只在 provenance。
**In scope:** wrapper、pin、registration、fake boundary。
**Out of scope:** live node、cloud、Nautilus fork、trading-system cutover。
**禁区:** 重写 EIP-712/nonce/WS/reconciliation。

### SB-004 — Read-only account and fee/funding mapping

**Outcome:** AccountPort/FeePort 表达 equity/margin/position/PnL/fee/funding/liquidation，并区分 actual/estimated。
**验收标准：** clearinghouse 映射完整；fee/feeToken/time/tid 幂等；funding 与交易费分开；negative rebate 可表示；liquidation event 与 liquidation fee absence 分开；schedule provenance 有 timestamp/hash；unknown 不归零。
**In scope:** fixture、normalizer、reconciliation receipt。
**Out of scope:** fee optimization、strategy fee logic、live calls。
**禁区:** fee 写入策略、base fee 覆盖 actual、伪造 liquidation fee。

### SB-005 — Order/protection serializer and capability gaps

**Outcome:** canonical order/protection semantics 可验证，native gap fail closed。
**验收标准：** limit/GTC/IOC/ALO/reduce-only；market=IOC+slippage+BBO；TP/SL/mark/normalTpsl/positionTpsl；fixed vs position-follow；partial-fill gap；modify/CLOID race fixture；FOK/GTD 不静默降级。
**In scope:** pure serializer、fake parser、capability tests、ambiguous outcome。
**Out of scope:** live submission、credential、strategy、Paper repair。
**禁区:** IOC 宣称 native market、normalTpsl 宣称 universal OCO、timeout 无条件 retry。

### SB-006 — Conformance, security and Paper boundary

**Outcome:** adapter conformance matrix、安全检查和 Paper-only evidence 完成。
**验收标准：** fake accepted/resting/filled/rejected/waiting/unknown；partial/duplicate/reconnect/modify/cancel fixtures；no-network/no-secret test；unknown capability before transport；gitleaks/redaction；static 与 live/testnet evidence 分开；现有 gates 不变。
**In scope:** harness、security、evidence、paper tests。
**Out of scope:** testnet/mainnet/cloud/live smoke。
**禁区:** external state mutation、真钱、auto broker switching。

### SB-007 — Trading-system host integration contract

**Outcome:** trading-system 作为 composition root 使用 provider-neutral standard-broker contract。
**验收标准：** host 只读 neutral contract；registry 使用 `(broker_id, environment, role)`；Paper gates 不变；recording 接 canonical order/fill/fee/provenance；Telegram 仍唯一控制面；PR 只提交 static contract evidence。
**In scope:** host contract、dependency direction、read-only Paper composition。
**Out of scope:** cloud、launchd/supervisor、live/testnet migration、strategy changes。
**禁区:** 修改 Park gates、自动启用 live client、绕过 reconciliation/immutable fill guard。

实施顺序：`SB-001 → SB-002 → SB-004 → SB-005 → SB-003 → SB-006 → SB-007`。每个 story 一个 Issue、branch、PR；merge 后从最新 `main` 开下一环；不部署、不碰云端。

---

## 12. 最终判断

应该复用：Hyperliquid 官方 docs/API semantics；官方 Python SDK 作为 wire reference；Nautilus v1.230.0 native adapter；Hummingbot 的 orderbook/user-stream/in-flight/fee patterns；Senpi 的 scanner/risk/telemetry separation patterns。

不应作为 v1 runtime dependency：CCXT、整个 Hummingbot、Hummingbot MCP/Condor、Senpi managed runtime/MCP、社区 Hyperliquid MCP、个人 LLM bot；也不应在 v1 接 HIP-3/Spot/HIP-4。

**一句话：Hyperliquid 是值得接入的 broker platform；`standard-broker` 的工作不是再造一个 Hyperliquid bot，而是把 Hyperliquid/Nautilus 的真实 broker 能力收敛成 canonical ports、显式 capability、可审计 provenance、可处理 ambiguous lifecycle，并在 Paper/live 边界上 fail closed。**
