# trading-system

## Operating documents (2026-08-02)

- Stable intent and completion evidence: [`NORTH_STAR.md`](NORTH_STAR.md).
- Current handoff snapshot: this file (`REGISTRY.md`); do not duplicate its
  history in the North Star.
- Material daily deltas: [`daily/`](daily/), using
  [`daily/YYYY-MM-DD.md`](daily/YYYY-MM-DD.md) as the format template.
- Durable rationale and traps: [`decision-log.md`](decision-log.md).

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(权威实施基线:docs/plans/implementation-plan-2026-07-24.md,完整产品 65% 评估)

## 现在在哪里(2026-08-17)
- #711 / #713 / #715 are complete on exact clean
  `main@7d7491a3507f1897cfd6909778526ca24859c313`.  Opening
  `https://goldbot.park-ai-intel.com/` now requires no email or OTP and lands
  on the dedicated Park Paper observer.  DNS routes only this hostname through
  the local `topic-workbench` Tunnel to the loopback Gateway; Goldbot v5 has the
  scoped `Public Paper observer / Bypass / Everyone` policy.  The Gateway
  exposes only the observer HTML, observer read model, and read-only session
  route, rejects every mutation method before body parsing, closes rejected
  HTTP/1.1 connections, and returns 404 for the legacy Dashboard and neighboring
  APIs.  Public verification passed 8/8 HTML/API/POST probes plus 12/12 repeated
  POST denials; a fresh browser rendered `事实完整` with zero buttons, forms,
  inputs, or links.  Dashboard/Gateway launchd jobs, predeploy, Dashboard boot,
  and Park safety evidence all bind the exact SHA.  The active neutral Grid
  identity remains unchanged (4100–4450, max 20x), reconciliation is `ok`, and
  safety records `operations.mutations=[]`.  Immediate rollback is to detach
  the public observer policy and save, restoring OTP; the hostname can then be
  routed back to `gridmind-paper-cloud` if origin rollback is also required.
- #715 closes the public Gateway's HTTP/1.1 origin connection after refusing a
  request body.  The Gateway still returns 405 before parsing or forwarding the
  body, but Cloudflare can no longer reuse that socket and reinterpret leftover
  bytes as a malformed follow-up request.  A raw pipelined-socket regression
  proves there is exactly one 405 response and no second request; the broader
  Park/Gateway/Dashboard suite passes (185).  Exact-SHA public deployment and
  repeated anonymous POST evidence remain runtime verification, not repository
  truth.
- #713 / PR #714 adds a dedicated immutable Park Paper public observer.  Its
  read model projects only persisted Park identity/plan/lifecycle/safety facts
  and the authoritative Nautilus Paper snapshot; it does not construct an
  adapter, refresh market data, reconcile, call a provider, write a file, or
  issue a control action.  The self-contained page has no controls and the
  public gateway admits only its exact HTML, read-model, and session GET
  routes; every POST is refused before body parsing.  Focused Park, gateway,
  Dashboard, and boot tests pass (184), Ruff/compile/diff/gitleaks pass, and a
  read against the current local Paper artifacts returned the active neutral
  Grid with reconciliation `ok` while artifact bytes remained unchanged.  The
  code does not by itself remove Cloudflare OTP or prove a public deployment;
  #711 retains that exact-SHA origin and edge-policy verification.
- #705 records the current Park Paper handoff after #701 / PR #701 and #702 /
  PR #704.  `main@84938268ba439ed5b9796000d7ff674d53f5bce3` now binds source-
  bound safety evidence and lets independent clean-slate strategy sessions share
  a Beijing 09:00/21:00 recording window without losing exact ownership.  The
  local `com.wendy.trading-orchestrator.park-paper-control` launchd job is
  loaded with a 60-second interval; the latest source-bound run is
  `status=pass`, `execution=active`, `next_action=continue_trusted_fresh_ticks`.
  The confirmed neutral Grid has 30 accepted Paper orders, zero fills, zero
  positions, equity `10000.0`, and reconciliation `ok`; a duplicate pass added
  no orders.  This is local Paper-only evidence, not live/real-money readiness.
  Park tests are `114 passed`; the full suite is `3028 passed, 1 skipped` with
  11 pre-existing macOS `schedule_manager`/launchd harness failures.  No live,
  Cloud, Feishu, Shadow, autonomous, exchange-key, or Telegram strategy state
  was changed by the recovery work.
- #707 / PR #708 fixes the remaining multi-session package-close fact selection:
  when a reporting window contains a terminal older session and a newer active
  session, the package now selects the active identity's latest positions fact
  and preserves `strategy_open=true`; package close remains review-only with
  `execution_mutations=[]`.  Merged on
  `main@e6a3104b5bc796ca437480c0bc8caa1c818cdfe4` after 115 focused Park tests
  passed and gitleaks found no leaks.
- #701 / PR #701 repairs the Park cutover safety evidence path.  It builds
  source-bound release, boot, immutable-fill, Paper-only, and Supervisor
  fail-closed evidence before any runtime mutation; missing or stale evidence
  remains fail-closed.  Merged on
  `main@621832923e071bf32bfc5571370b842ddaa06be0`; focused Park validation and
  gitleaks passed.  This removed the observed `park_cutover_blocked` state once
  the exact local Paper source/boot receipts were present.
- #702 / PR #704 decouples Recording Track from execution identity.  Distinct
  clean-slate `strategy_session_id`/`strategy_revision_id` pairs can share one
  12-hour reporting window; same-pair starts remain idempotent and half-bound
  identity reuse remains fail-closed.  Multi-session packages and late
  amendments preserve all identity pairs and never emit execution mutations.
  Merged on `main@84938268ba439ed5b9796000d7ff674d53f5bce3` after 114 focused
  Park tests and gitleaks passed.
- #696 / PR #697 adds bounded Park confirmation shortcuts.  Park may reply
  `确认当前计划` / `确认这个计划` (or a bounded English equivalent) and the
  router resolves it only against the single unexpired proposal for the active
  session/revision; the exact digest command remains supported.  Codex remains
  an untrusted intent parser and cannot authorize execution.  An expired
  unconfirmed proposal is released only after Paper snapshot and reconciliation
  prove a clean slate; exposure or uncertainty stays blocked.  Merged on
  `main@eeb130220185f412be25663f6e6b2860c1e2e7b6` after 59 focused Park tests
  passed and gitleaks found no leaks.
- #692 / PR #693 repairs Park Telegram account admission.  The default reader
  now uses the direct Park-authoritative Paper adapter and receives the same
  external Paper config as `pipelines/park_control.py`; it no longer calls the
  legacy DualTrack/Shadow-gated configured adapter.  This removes the observed
  `Park strategy is blocked: RuntimeError` path without weakening any gate.
  Merged on `main@7e25262dc22b27ab4c8d8652d59a940862e35321`; local read-only
  Paper smoke reported equity `10000.0`, zero open positions, zero accepted
  orders, and healthy reconciliation.  No Telegram strategy was submitted
  and no order/position mutation occurred.
- #686 / PR #687 makes `neutral`/`中性` a first-class Park direction for Grid.
  The Telegram path now creates a deterministic bilateral proposal from
  messages such as `中性网格策略 4450 4100 最大20x杠杆`, sizes both explicit buy
  and sell legs under the stricter leverage/loss cap, and requires Park's exact
  confirmation before the authoritative Paper adapter receives any command.
  A trusted boundary touch freezes/cancels only owned entries, preserves
  neutral owned positions for reconciliation, records closure and pauses;
  there is no reverse, reopen, live, Cloud, Shadow, Feishu or autonomous path.
  Merged on `main@e5ac71a82af366dba55e1556dff0c084498322a` after 40 focused Park
  tests passed and gitleaks found no leaks.
- #682 / PR #683 fixes the launchd Codex timeout handoff.  The bounded provider
  window is now 30 seconds (the observed Codex response is about 14–18 seconds),
  and a clear `中性网格` message has a deterministic, non-authoritative fallback
  when the provider is slow.  The fallback now feeds the same neutral Grid
  proposal/risk path; it never maps neutral to long/short or bypasses exact
  confirmation.
  Main is merged at `9e6f92d970f92b4f857fdde31a762326f3ea15a9`; the local
  `com.wendy.trading-orchestrator.park-paper-control` job is source-bound to the
  same Paper-only code path and latest run is `status=pass`, `execution=idle`.
- #678 / PR #679 connects the local Telegram ingress to a bounded Codex CLI
  intent parser.  Codex receives only Park's strategy text and returns an
  untrusted candidate; deterministic normalization, trusted market/equity
  risk planning, exact Park confirmation, and Paper execution remain the sole
  authorities.  Natural-language `neutral Grid` is recognized and reaches the
  bilateral proposal path, rather than being mapped to long or short.  The
  local launchd handoff remains
  Paper-only, Telegram-only, and source-bound; no live, Cloud, Feishu, Shadow,
  autonomous, or credential path is enabled.
- #672 / PR #673 and #674 / PR #675 complete the attended local Park Paper
  handoff on exact `main@4851718746df63481ce59ea351cbd006f0917eb8`.  The
  local `com.wendy.trading-orchestrator.park-paper-control` launchd job is
  loaded with a 60-second poll interval and the latest source-bound
  predeploy, boot, and Park preflight receipts are `pass` /
  `ready_for_park_paper`; the Telegram worker safely rejected an incomplete
  `/start` with zero orders and zero positions.  This is Paper-only and
  local: no live path, exchange credential, Cloud deployment, Feishu, Shadow,
  or autonomous strategy path is enabled.
- #668 / PR #670 adds the isolated Park Paper runtime and bounded
  Telegram+Paper control pass.  Only an exact, unexpired Park confirmation
  for the current session/revision/plan digest can reach the direct
  `nautilus_paper` adapter; every command and terminal action carries Park
  ownership.  A trusted/fresh boundary touch cancels only owned entries,
  handles only owned exposure, reconciles, closes the session and pauses;
  09:00/21:00 Beijing recording windows only package/review and never switch,
  replan, cancel or flatten.  The repository default remains fail-closed;
  the separate local Paper config is the only explicit enablement used by the
  attended launchd handoff above.
- #667 / PR #669 adds the Telegram Bot long-polling transport, durable cursor,
  explicit `message_id` receipts, Park proposal router, exact digest
  confirmation handling, duplicate/conflict audit and single worker lease.
  It is a pre-execution seam: no broker mutation, deployment, Cloud change,
  live path, Feishu, Shadow or autonomous start is enabled.
- #665 adds the default-off, read-only Park cutover/release gate.  Explicit
  enablement still requires one Paper track, Telegram-only control, no
  Autonomous/Shadow/Feishu mutation, exact release SHA and boot proof, and all
  trusted-market/tick/stale-state/reconciliation/immutable-fill/Park-risk/
  Paper/SHA/boot/Supervisor gates.  Repository pass is not runtime readiness;
  no current Supervisor, deployment, Cloud, order, or position path changed.
- #663 adds the independent Park Recording Track.  Each Beijing 12-hour
  manifest binds the same strategy session/revision across windows, records
  control/plan/order/fill/position/exit/Telegram/provider/tick/runtime/
  reconciliation/execution facts, allows open strategy/positions, and marks
  missing evidence blocked.  Late events append package amendments; recording
  performs no switch, cancel, flatten, replan, deployment, or Cloud mutation.
- #661 adds the Park Grid fixed-range lifecycle.  Levels are deterministic,
  identity-bound and unchanged while price remains inside the authorized range;
  trusted/fresh boundary touch terminates once and notifies Park.  Geometry or
  direction changes are proposal-only and require a new clean-slate revision
  and confirmation; no autonomous replan or broker/runtime mutation is added.
- #659 adds the Park DCA lifecycle adapter.  Finite DCA entries carry exact
  session/revision/plan ownership and require the exact confirmation receipt;
  trusted/fresh touch of either authorized boundary yields one idempotent
  terminal action plan and Telegram notification, with no loop or reopen.
  The adapter emits commands/records only and does not call a broker or alter
  runtime/Cloud state.
- #657 adds the exact Park confirmation capability ledger.  A proposal binds
  session/revision, plan digest, risk digest and expiry; only Park's exact
  confirm/reject command can decide it; duplicates are idempotent and stale or
  expired replies fail closed.  A confirmed receipt is not a start/order and
  has no runtime, broker, deployment, or Cloud side effect.
- #655 adds deterministic Park input normalization and Paper risk planning:
  Chinese/English DCA/Grid and range/leverage input are canonicalized; trusted
  fresh market and authoritative Paper equity are mandatory; leverage and
  maximum-loss caps select the stricter notional constraint; no direction,
  stop, equity, or authorization is invented.  The planner is pure and does
  not place orders or mutate runtime/Cloud state.
- #653 adds the durable Telegram Park control-plane ledger.  It authenticates
  one Park user/chat, persists idempotent inbox updates and outbox messages,
  requires explicit transport receipts, retries bounded failures, dead-letters
  terminal delivery failures, and rejects stale strategy bindings.  The ledger
  grants no execution authority and has no network, Feishu, order, position,
  deployment, or Cloud side effects.
- #651 adds deterministic Park ownership and lifecycle planning on top of the
  independent session identity.  Clean-slate admission, exact order/fill/
  position ownership, immutable active revisions, idempotent trusted-boundary
  terminal action plans, and structural no-flatten blockers are now explicit;
  the module emits plans only and does not call a broker or alter legacy state.
- #649 adds the default-off Park Strategy identity layer: immutable
  `strategy_session_id`/`strategy_revision_id` remain active across independent
  Beijing 09:00/21:00 recording windows, whose append-only amendments have no
  strategy-switch, replan, cancel, or flatten authority.  The layer is isolated
  from the existing cycle runtime; no orders, positions, Supervisor, rollover,
  deployment, or Cloud state changed.
- #647 establishes the approved, default-off repository contract for the next
  Park Strategy Track: exactly one Paper execution track, Telegram as the sole
  future control plane, clean-slate-only admission, an immutable active
  strategy, and automatic terminal close only when a Park-confirmed price
  boundary is touched.  Strategy session/revision identity is independent of
  the Beijing-time 12-hour recording window.  This issue changes no runtime,
  order, position, deployment, or Cloud state; implementation wiring remains
  deliberately absent and must proceed through later atomic Issues.
- #642 / PR #643 and #644 / PR #645 are merged and deployed on Cloud Paper as
  exact clean `main@29ecd3e83b54d129bfba46fc827c228797ead1b`, tree
  `51d2c465a281cd98eeff8da67cd31b5247cbeb51`; the source symlink was switched
  atomically without restarting Paper services. #642 makes the deterministic
  current-market + authoritative-Paper-equity rebuild the explicit Paper
  boundary guarantee (`boundary_plan_path=deterministic_rebuild`) and makes AI
  next-cycle pre-generation an asynchronous optional enhancement. #644 adds a
  durable provider-call trace to readiness/evaluation receipts: deadline,
  start/deadline/finish timestamps, phase timings and return codes, bounded
  redacted stdout/stderr, timeout and partial-output flags. A natural readiness
  run at 00:56 CST was source-bound to this SHA and recorded the provider's
  60-second smoke timeout (`login=1.56s`, `version=0.22s`, `smoke=60.10s`)
  with bounded stderr and partial-output evidence; this remains a readiness
  warning, not a Paper boundary blocker. Paper runtime remained
  `running/running`, 19 accepted/open orders, zero positions, fresh tick and
  passing reconciliation; no manual controls occurred.
  Paper/Live safety gates are unchanged.
- #642 / PR #643 was previously merged at `main@2a0f6d6c8b8f79344d02fa1977aafcfc1f4d6ead`.
  It makes the deterministic current-market + authoritative-Paper-equity
  rebuild the explicit Paper boundary guarantee (`boundary_plan_path=
  deterministic_rebuild`). AI next-cycle pre-generation is now an
  asynchronous optional enhancement: explicit provider execution/configuration
  failures return `enhancement_unavailable` with zero control/plan/order/
  position mutation and the boundary remains on deterministic rebuild. Valid
  staged artifacts remain eligible only after all existing StartFacts, policy,
  source, identity and zero-mutation checks, and are marked
  `boundary_plan_path=optional_ai_enhancement`. Strategy evaluation receipts
  now persist bounded redacted provider deadline/timestamps, phase timings,
  return code, stderr/stdout and timeout partial output. Paper/Live safety
  gates are unchanged. This exact commit is merged but not yet deployed.
- #636 / PR #637 is merged at `main@b7bbbd65fbd902de871766531a2a10a35d060716`.
  It bounds the next-cycle recommendation prompt to the explicit
  `strategy-ai-account-context-v1` projection (authoritative account,
  reconciliation, and open execution identities; no historical fills/trades)
  and gives the read-only pre-generator its own bounded 60-second provider
  budget while keeping the Supervisor budget at 25 seconds.  It is deployed
  on Cloud Paper as part of exact clean `main@16c19091916e066212bf2604e52d380c0f0cf85a`,
  tree `1edcda756bd45655018ed73dc22dae3608d98bcd`; the first natural due-path
  run after release remains the acceptance gate.
- #639 / PR #640 is merged at `main@16c19091916e066212bf2604e52d380c0f0cf85a`.
  It gives the read-only AI readiness smoke its own bounded
  `provider_readiness_timeout_seconds=60` while preserving the 25-second
  Supervisor/live-tick budget and all fail-closed readiness checks.  The
  Cloud receipt at 22:08 CST is `status=pass`, source-bound to the exact SHA
  and tree, with logged-in provider, zero controls, and no order/position
  mutation.  Natural next-cycle due-path and boundary evidence are still
  pending.
- #631 / PR #632 is merged and deployed on Cloud Paper as exact clean
  `main@57959a3389c4be9745bbb18bdb6b4fa831713077`, tree
  `0aabb4660b7b200f5bcc64799996b84169f8967a`.  It repairs the two production
  blockers exposed by the first natural #439/#413 acceptance.  At 08:04 CST the due path of
  `gridmind-next-cycle-plan.service` reached its 128 MiB cgroup ceiling and was
  OOM-killed, so the 09:01 boundary had no verified successor and correctly
  used the existing safe close fallback.  The same boundary then exposed an
  intra-tick authority split: candidate and `prepare_start` independently read
  the still-forming 1m bar, producing different exact StartFacts digests and a
  repeating `start_facts_stale` result.  The scoped repair raises only this
  measured unit to `MemoryHigh=192M` / `MemoryMax=256M` and pins one trusted
  market snapshot across candidate/validation/prepare inside a Paper-continuous
  Supervisor tick; `start` still reads fresh market and all live/fail-closed
  behavior is unchanged.  At 09:35:32 CST the first natural post-release
  Supervisor tick recovered `2026-08-13_DAY` to `running` with 38 accepted/open
  orders, zero open positions, passing reconciliation, a fresh tick and zero
  manual strategy controls.  At 09:38 the next-cycle unit naturally completed
  its `not_due` path under the new limit with zero controls and no new OOM.
  The first repaired due path around 20:00 and the natural 21:00 adopted
  identity-preserving handoff remain the explicit acceptance gates.
- #439/#413 cross-cycle handoff repair merged as PR #630 and is deployed on
  Cloud Paper at exact `main@97cef3c3777ee7674c730b13e50e4583029326dc`.
  The first natural 2026-08-13 DAY acceptance did not pass because #631's
  pre-generation OOM left no waiting artifact.  The immutable boundary event
  recorded `outcome=missing`, `boundary_ai_provider_calls=0`; rollover used
  `safe_stop_cancel_flatten`.  All nine positions had already closed naturally,
  but 38 remaining orders were canceled, so identity-preserving handoff remains
  unproven pending a later natural boundary.
- #623 adds the missing containment for administrator-session processes:
  source-controlled `user-.slice` defaults load `MemoryHigh=384M` and
  `MemoryMax=512M` without including SSH, cloudflared or system GridMind units.
  The independent dead-man watcher also observes cgroup `memory.events` and
  emits one deduplicated external failure for a new pressure/OOM increment.
  Every managed unit now has a Cloud cgroup sample and an evidence-backed limit
  in `docs/operations/cloud-unit-memory-calibration-2026-08-12.md`; live tick
  uses 384/512 MiB and backup 256/384 MiB so reclaim happens before the hard
  kill boundary.  Focused tests pass; merge, exact-main deployment, loaded
  property checks, fresh SSH and one isolated external canary remain required.
- #622 fixes the first scheduled post-hardening 24h-report delivery failure
  without weakening file or systemd permissions.  Cloud systemd already
  injects the root-owned Paper environment before switching to `gridmind`;
  report/trade/alert senders now use those injected values instead of reopening
  the 0600 file.  Non-Cloud loading is unchanged.  It is deployed at exact
  `main@17f8d6258918cf640f03a782a01d3cbe5fe6ab1c`; the permission error is gone,
  but the attended rerun proved the Cloud report notifier itself was never
  configured.  That separate explicit delivery contract is now #628.
- #621 removes the dead-man's cycle-sized memory growth before release: Cloud
  health now reads the fsynced bounded Supervisor checkpoint/tail rather than
  materializing the full current-cycle JSONL.  The exact 2026-08-11 NIGHT
  production shape was 28 MiB / 509 observations and pushed the deployed
  dead-man to 201,322,496 bytes, only 4 KiB below its 192 MiB cgroup ceiling;
  the patched isolated full-dead-man replay completed at 73,060 KiB maximum
  RSS with zero production mutation.  A separate one-minute watcher now owns
  dead-man receipt/timer liveness and emits only deduplicated external `/fail`
  signals, never a success ping.  It is deployed at exact
  `main@17f8d6258918cf640f03a782a01d3cbe5fe6ab1c`: natural dead-man peak fell to
  17 MiB immediately after release and 67.17 MiB in the later natural sample;
  the primary heartbeat and independent watcher both recovered naturally.
- #612 removes the two audited whole-file SHA-256 allocations from Cloud Paper
  backup and daily self-review.  Both now share a fixed 1 MiB `readinto()`
  buffer while preserving exact digest/manifest semantics; a 64 MiB sparse
  fixture verifies that peak Python allocation stays bounded rather than
  scaling with artifact size.  This closes the production-code recurrence
  mechanism but does not make arbitrary admin-session scripts safe (#613).
- #611 adds source-controlled OOM containment for the 1.6 GiB Cloud Paper
  host: the exact loaded Ubuntu `ssh.service` and Cloudflare tunnel are
  protected with `OOMScoreAdjust=-900`; managed Python units have explicit
  per-role `MemoryMax`; and each bounded failure routes through a non-recursive
  systemd helper that fsyncs a structured Paper-only event before signaling the
  external dead-man `/fail` endpoint.  Kernel/auth/service journal attribution
  corrected the incident narrative: the 2026-08-11 direct OOM victim was an
  abandoned interactive `sudo python3 -` process in `session-3051.scope`, not
  a GridMind unit; backup had completed hours earlier.  #612 still removes the
  two production whole-file hashing risks, while #613 separately owns global
  batch/operator admission and #614 owns the single-host failure domain.
- #605 is merged through PR #606 and deployed on Cloud Paper as exact clean
  `main@5c0cf593eab8dac34357a1d9794a729b4cade079`, tree
  `b519bf93833e322b5170d42413b4e2da4db98dae`. The 2026-08-09 DAY stop was
  caused by the Paper-continuity lineage verifier rejecting normal
  control-plane `confirmed` execution fields beside the exact selected
  `paper_continuity_recovery` source. The scoped fix retains exact
  proposal/package/recovery/root-AI digest checks and default fail-closed
  behavior, while allowing that mixed lineage only in the explicit Paper
  recovery calls. After the source switch at 12:39:58 CST, a natural Supervisor
  attempt created one fresh preview/prepared start and reached `running_proven`
  at 12:40:34 CST: Grid neutral, 38 planned grids, 38 accepted/open orders,
  zero positions and passing reconciliation. Cloud health is `healthy`, all
  seven timers are active, required exact-SHA boot receipts pass, and no human
  strategy control occurred. The hash-linked watchdog/recenter degradation
  events and full deployment evidence are preserved in #605; this proves the
  target-environment repair but does not replace the outstanding conservative
  48-hour continuity acceptance.
- #602 applies the repository's established missing-GitHub-metadata rule to
  #597/#598. Their PR objects are not usable audit credentials while API
  readback returns 404; exact implementation/merge SHAs, committed trees and
  `main` ancestry are retained in
  [`docs/audits/github-provenance-597-598.md`](docs/audits/github-provenance-597-598.md).
  This is the same bounded provenance limitation already indexed for
  #52–#128 and #222–#329, not a one-off failure diagnosis or a code-loss claim.
- #594/#595/#596 implement the shared architecture law behind the 09:12 start-shape
  mismatch and #593's review-count mismatches: compute one authoritative
  domain fact once and make every consumer reference its digest.
  `paper-start-facts-v1` now binds trusted market, authoritative Paper
  execution equity/snapshot, execution contract, exact Park policy and clean
  source before proposal/preview identity. A dedicated non-overlapping Cloud
  timer can now use those exact facts to generate one immutable
  `verified_waiting` successor-cycle candidate during the final hour of a
  proven-running cycle, with zero active-plan/prepared-start/order mutations.
  At the boundary Paper Supervisor either adopts its exact projected plan with
  `boundary_ai_provider_calls=0`, or records why executable facts changed and
  uses the existing deterministic current-market/authoritative-equity builder;
  missing or corrupt staging never reintroduces an AI call at the boundary.
  Systemd boot/timer/soak invariants and read/package evidence include this new
  lifecycle. Before any pre-intent exception is reduced by the public
  fail-closed classifier, #596 now appends a source-bound, hash-linked,
  sanitized receipt with exact attempt/phase, typed code, bounded cause chain
  and stack fingerprint. Attempt/degradation evidence references its digest,
  while Supervisor history and the terminal 12h package expose the verified
  chain without provider payloads, prompts, credentials, local paths or raw
  tracebacks. #596 is merged through PR #599 at
  `main@ed99a6ad79daf44d9606c2ec7b974ff61c9b8f7d`; it has not been deployed and
  is not #588 continuity acceptance. #593 remains the separate
  execution/review-output authority repair.
- Milestone 3 fixes Paper continuity in four sequential stories. #585 adds the
  prerequisite immutable degradation-event chain, v2 12h package projection,
  and sealed stopped-to-`running_proven` transition gaps. #586 adds the closed
  `execution_profile`, proven-Paper-only composition, clean per-cycle state,
  and a crash-durable five-minute watchdog that clears recorded blockers and
  keeps converging for the full cycle. #587 implements the recovery adapters:
  every Paper watchdog Grid or DCA candidate is rebuilt around current trusted
  market and authoritative execution equity; per-grid/per-addition notional is
  deterministically capped inside the exact Park boundary; AI outages inherit
  only an exact current plan or the immediate prior verified terminal package,
  with every proposal/package digest recursively verified to the persisted AI
  root rather than trusting self-declared lineage; and an expired
  strategy policy can be continued only through an append-only, clean-source-
  bound, 24-hour Paper receipt that never edits or widens Park's policy. Each
  alternative is journaled before `start_intent`, and every bypass capability
  is bound to the exact same-cycle, same-attempt event id+digest frozen into
  `prepared_start`; fail-closed and live paths do not accept these receipts,
  envelopes, or fallback flags. Checked-in configuration remains
  `fail_closed`, so these merged stories still do not activate a bypass or
  change current Cloud behavior. Post-`start_intent` unknown/partial outcomes
  remain non-resettable and require authoritative reconciliation before any new
  attempt. Active and new-cycle DCA recovery both use the family-preserving
  exact-policy adapter and audited Paper-only confirmation capability rather
  than being converted to Grid.
  #588 alone activates and deploys
  `paper_continuous`, then holds the milestone open until both 24h and 7d
  conservative utilization reach at least 85%. Shadow and the known review
  chain discrepancies remain explicitly outside this milestone.
- #573/#574/#581 are deployed on Cloud Paper as exact clean
  `main@28cc8bd888aee06ba8fb89e03c94fa249629471e`, tree
  `15b393d8be70f97f1d5c431e12b096b7d70cc35e`. The canonical host-local
  provider-readiness timer is enabled/active, runs as `gridmind`, polls without
  overlap every five minutes, and renews the 24-hour proof at six-hour age.
  Its real timer contract passes with exact load path/content hash, monotonic
  next-trigger evidence, and digest/source-bound current/latest-success
  receipts. All 10 required services/timers are active; natural dead-man
  delivery succeeded with zero failure-signal sources. Read-model is HTTP 200:
  current runtime remains `running/running` on the same 38-slot plan, with 38
  accepted orders, 37 open orders, one open Paper position, 80 fills, zero
  unknown orders, reconciliation `ok`, trusted/fresh market, ready tick and
  Supervisor, `provider_readiness_timer_ready`, warning-only Cloud health and
  zero critical incidents. Control audit recorded zero deployment-window
  actions. Evidence:
  [#574](https://github.com/zinan92/trading-system/issues/574#issuecomment-5200670920),
  [#581](https://github.com/zinan92/trading-system/issues/581#issuecomment-5200671376).
  This is deployment/continuity evidence, not the still-open 48-hour acceptance.
- Park-authenticated Cloud authoring has appended immutable
  `park-grid-paper-v2` version 2 with exact allowed directions
  `long/neutral/short` (policy digest `217e4c3e...4013`) and
  `paper-supervisor-grid` binding version 2 (binding digest
  `38e57cb3...dcdb`). Both authoring mutations created zero plans, controls,
  orders, or positions. #575/#576 are deployed on Cloud Paper as exact clean
  `main@d7a014479ee8d4c5cbec46aea582fc289212ce89`, tree
  `e6a91d8ced5c0f341b52dc37a4cb91d50a59200a`; Cloud preflight, provider
  readiness, Paper predeploy, Dashboard boot, and natural live-tick boot all
  passed at that SHA/tree. The first natural tick cleared the prior v1-bound
  structural rejection with zero control actions. A later natural attempt
  typed `strategy_recommendation_provider_timeout` as transient and backed off;
  the subsequent independent tick used fresh preview
  `grid-preview-3c0648ccc797` and prepared start
  `prepared-start-3bc94010e01a58e7`, then reached `executed` at 11:06 CST.
  Two more natural ticks preserved one exact 38-slot neutral Grid: runtime
  `running/running`, 38 accepted/open orders (19 buy + 19 sell), zero unknown
  orders and positions, fresh heartbeat, reconciliation `ok`, and Supervisor
  `adopted_existing`. The authenticated Dashboard independently showed
  `运行中` and `38 笔已接受委托`. This is current-cycle recovery evidence, not
  the still-open 48-hour >=85% utilization acceptance result. Provider
  readiness currently expires on 2026-08-07 and the Grid-only standing policy
  still expires on 2026-09-01; both remain explicit autonomy follow-ups.
- #568 removes the current-cycle `no active plan -> supervisor_not_required`
  health gap. No-plan and active-plan/stopped states now share an exact
  300-second convergence window anchored to the cycle boundary or the latest
  matching sealed running proof; attempts, observations, clearance events and
  restarts cannot reset it. Stalled, corrupt/unknown, structural and
  alert-required states drive dead-man critical, while fresh legal
  backoff/probing and the utilization ramp remain non-critical. This is
  implementation evidence only; exact-main Cloud deployment and v2 selector
  activation remain separate.
- #567 preserves recommendation-provider failures as exact typed evidence from
  `RecommendationProviderError.code` through the immutable failed evaluation
  receipt, Dashboard/control boundary, Supervisor whitelist, and legacy
  structural recheck. Untyped prose, missing/malformed codes, and legacy
  error-only receipts remain `unknown_blocker`/structural; the fifth exact
  transient failure enters the existing nonterminal probe/alert mode with zero
  plan, control, or order side effects. #567 is deployed code-only as exact
  clean `main@33562a48ae3ee2fcb9f3dcd9942e91151d67a7f1`, tree
  `207f705f3c8e9ee67a918e9167209daeab5bb184`; the next natural tick adopted
  the unchanged 36-slot neutral Grid with zero control actions. The v1 selector
  remains active, so this is not v2 activation or 48-hour acceptance.
- #566 binds `outer_strategy_policy_envelope_out_of_bounds` to an immutable,
  deny-only rejected-candidate receipt instead of clearing it merely because a
  policy exists. Recheck is read-only and requires a different exact Park
  binding, every stored candidate comparison passing, fresh/clean zero-exposure
  authority, exact WAL-attempt/receipt linkage, a strictly verified recheck
  proof, and no unknown control outcome. A pre-authorization candidate marker
  closes the receipt-fsync crash window, and every cleared rejection remains a
  cycle tombstone against proposal/preview/facts/confirmation reuse.
  Missing/tampered/legacy evidence remains structural; legacy clearance
  requires a separately signed Park resolution and still executes zero control
  actions. This is implementation evidence, not v2 selector activation or the
  48-hour acceptance result. #566 is deployed code-only as exact clean
  `main@d5dd0715c16de0c00b087e5ef2deabf103ed30b0`, tree
  `571208d072d73a1788085b9399693fc9ccf227b4`; all release/boot gates passed,
  the natural live tick adopted the unchanged 36-slot Grid with zero control
  actions, and the v1 selector remains active.
- #565 is deployed on Cloud Paper as exact clean
  `main@64e5f7149aa27c639e454e93631f1fbbc4a4c808`, tree
  `ca5d34610f1de83986cb18cee7baae3e6a77abf4`. Candidate preflight, provider
  readiness, predeploy, Dashboard boot, and the next natural live-tick boot all
  passed for that SHA/tree. The running NIGHT Grid remained 36/36 logical slots
  (34 accepted orders + 2 positions), reconciliation `ok`, market trusted/fresh,
  and Supervisor `adopted_existing`; release control actions were zero. The
  current v1 selector remains authoritative because the cycle has exposure;
  v2 activation must wait for an exact zero-exposure cycle boundary and a
  separate config SHA release.
- #565 introduces an immutable `paper-strategy-policy-boundary-v2` for an
  explicit, canonical Paper Grid direction set. The existing v1
  `direction=neutral` meaning and every prior record/digest remain unchanged;
  v2 persists membership comparison rows and still rejects DCA, numeric
  overflow, malformed direction sets, forged actors, and unknown schemas before
  plan/order creation. V2 accepts no caller-supplied limits or expiry: it copies
  both from the current exact v1 binding and persists that immutable inheritance
  proof. Policy/binding authoring no longer depends on unrelated
  market/account reads, but code availability is not authorization or
  activation: the Cloud remains on its exact v1 binding until Park-authenticated
  v2 append plus a separate SHA/boot-gated configuration release.
- #560 已部署为精确 clean
  `main@3a9d06d5e870597dc319ce9a2b97fc9f8ad49ea3`，tree
  `d41fa1f8bf24d6b6afd5b9e8895bc92b757e7705`。Cloud preflight、provider
  readiness、Paper predeploy 与七个 boot gate 均绑定该 SHA/tree 并通过；
  datafeed、Dashboard、Access Gateway、cloudflared 与五个规范 timer 均
  active/enabled。Cloud health 为 HTTP 200 / 15.937s，低于 dead-man service
  的 30s 硬超时；15:05 CST 的自然 dead-man tick 已真实投递 HTTP 200，
  `target_kind=success`、无 critical failure signal。
- 部署前后 2026-08-04 DAY 始终为同一 Grid
  `strategy-plan-2026-08-04_DAY-2-2060ce1e` v2、runtime=`running/running`、
  34 个 accepted/open orders + 2 个 exact open positions = 36 个授权逻辑格位、
  100 raw fills、行情 `ready/fresh/trusted`、执行与会计对账通过，Supervisor 为
  `adopted_existing` 且无 blocker。orders/positions/fills 的精确摘要与 control
  audit=354 均未变化；本次发布执行交易控制动作=0。authenticated 公网 Dashboard
  已显示相同权威状态与完整 Supervisor 历史。
- #560 只证明 Cloud-health/dead-man 有界恢复，不等于 Supervisor 终验。首个
  完整 24h 运行率窗口尚未形成，当前 `insufficient` 不参与 critical 判定；
  连续 48h、保守运行率 ≥85%、三个真实周期边界及一次真实 transient 自愈链
  仍是未完成的唯一终验。显式完整 Supervisor 历史现为 HTTP 200，但在 306 条
  observation 时已达 26.745s / 19.0MB；这是独立增长风险，不回写为 #560 失败，
  也不得靠删减或改写不可变审计解决。
- 2026-08-04 11:59 CST 的 authenticated Dashboard 只读核验确认当前 DAY
  Grid 仍为 `running`，36 个逻辑格位由 35 个 accepted orders 与 1 个 exact
  open position 唯一代表，行情为实时可信，Supervisor 为
  `adopted_existing` 且无 blocker。点击 Supervisor 完整审计时，Cloud
  Access Gateway 对已由 Dashboard 与 Dashboard Server 共同实现的精确 GET
  `/api/trading-system/supervisor-history` 返回 404；根因是 gateway
  `ALLOW_EXACT` 遗漏该只读路径，不是 Supervisor store 丢失。#556 只补这一条
  exact allowlist 及相邻路径 fail-closed 回归；在精确 main SHA Cloud 发布、
  authenticated HTTP 200、完整审计可见且 Paper 权威状态前后一致之前，#472
  仍不得收口。
- Cloud Paper 当前精确运行 `main@274d6100d64a7267890cc7073bebac12f53c4a48`
  （#549），NIGHT Grid 为 `running/running`，38 个授权订单槽位中 1 个已成交为
  open position，其余继续挂单，账本与执行对账均为 `ok`，Supervisor 为
  `healthy/adopted_existing`。#549 已把旧 K 线按逐事件终态证据安全复用，但
  自然 timer 的约 72 秒有效间隔会让一轮同时出现 2 根新 1m K 线，首两轮仍各
  触发两次完整 Nautilus 回放，总耗时 10.56/11.41 秒。#550 正在把同一锁内的
  新 Grid 事件按时间顺序一次落盘、一次回放；DCA 继续逐事件，缺失/损坏证据、
  未处理 crash gap、行情信任闸和不可变成交守卫均保持 fail-closed。Dashboard
  本地 read-model 为 HTTP 200，公网 Cloudflare Access 为 302，隧道 active。
- #537 已部署到 Cloud Paper 精确 source
  `main@cdaadafd09c0835109170f075a67848c48fe7b30`：Cloud health 不再因
  runtime=`running` 提前跳过 Supervisor，运行中出现 structural episode
  仍会 fail-closed 并驱动 dead-man critical。#538 正在把默认五秒 Dashboard
  read-model 改为哈希锚定的有界 Supervisor 摘要，并将完整不可变审计移到
  点击 Supervisor 标签后才读取的独立只读接口；不使用 TTL、不改控制路径、
  不删除或压缩任何历史。部署后必须以公网 200、p95<5s、响应体积、CPU/RSS/
  线程和零 502 现场证据收口。
- #542 已部署到 Cloud Paper 精确 source
  `main@e910774573307ea975db9f100201ad3574b5f23c`，tree
  `c7c222e94e2a27f3731f3ff34f1c81139d2ed2b9`。19:48 CST 的第一拍自然
  tick 写入 `structural_cleared`，19:49 的下一独立 tick 写入
  `healthy/adopted_existing`；两拍均 v2 proof=`proven`、控制动作=0，始终为
  38 个逻辑槽位（37 accepted + 1 exact open position）、1 条 start intent /
  result、0 stop/cancel、unknown=0、对账通过。#537 正在修复 Cloud health
  曾被 runtime=`running` 提前短路、因而看不到 Supervisor structural 的告警
  漏洞；#538 仍负责 Dashboard 轮询 502。
- #540 已部署到 Cloud Paper 精确 clean source
  `main@f727401da65db2f7fbbe8b2bbbed4c7d103ccbf8`，tree
  `97ca748db11dded2eda4634dd1503e9090e839ca`。部署前 393 条不可变 DAY
  observation 全部按原 v1 公式通过；部署后首个自然 tick 成功写入 v2 并完整
  结束，`attempt_store_corrupt` 已消失，未改写历史、未执行控制动作。该 tick
  同时证明 v2 running evidence 为 `proven`（38 个授权槽位 = 37 accepted + 1
  exact open position），但既有 structural episode 仍粘滞为
  `order_identity_conflict`，因此 #542 正在补充该精确码的零控制 recheck；在
  自然 tick 留下 `structural_cleared` 与后续 `adopted_existing` 前不能称恢复。
- 2026-08-03 18:51 CST，当前 DAY Grid 的一个已授权 entry order 自然成交并
  成为 open position；执行与会计对账继续通过，runtime 仍为
  `running/running`，本次转换未产生第二次 start 或其他控制动作。该现场暴露
  #536：Supervisor 把 start 时接受的 38 个逻辑槽位误与成交后的 37 个 open
  orders 比较，因而错误记录 `order_identity_conflict`。#536 将计数改为与 38
  个授权槽位比较，同时继续逐槽要求“accepted order 或 exact open position”
  唯一代表，所有指纹、计划、成交谱系与双层对账闸保持 fail-closed。检测修复
  已部署且 v2 证明通过；既有粘滞 structural 状态的清障由 #542 独立完成。
- 当前 Cloud Paper source 为
  `main@f727401da65db2f7fbbe8b2bbbed4c7d103ccbf8`，tree
  `97ca748db11dded2eda4634dd1503e9090e839ca`。#532 已让完整可校验的
  Cloud-native health 成为 dead-man 的 liveness authority；2026-08-03
  18:23:34 CST 的自然 timer 实际投递 `success`、HTTP 200，
  `failure_signal=false`、`failure_signal_sources=[]`。warning-only 的
  `daily_self_review_missing_or_incomplete` 不再因服务器不生成旧本机 runner
  心跳而误送 `/fail`；Cloud critical/不完整 health、调度失败和暴露不确定性仍
  fail-closed。完整证据见
  [`docs/evidence/issue-532-cloud-deadman-success-2026-08-03.md`](docs/evidence/issue-532-cloud-deadman-success-2026-08-03.md)。
- #528 已部署到 Cloud Paper 的精确 clean source
  `main@66b832df6bea81cecdfa3d28ef74735736493bb8`，tree
  `a271a2deb832f8422fc613d124656cba966bb710`。Supervisor v4 在第一个自然
  tick 以 0 控制动作清除 2026-08-03 DAY 的已知 pre-intent
  `unknown_blocker`，下一独立自然 tick 使用全新 preview/prepared identity
  唯一启动，当前计划 `strategy-plan-2026-08-03_DAY-2-ffb31483` v2、runtime
  `running/running`、38/38 唯一 accepted orders、0 unknown、0 positions、
  reconciliation=`ok`；后续自然 tick 为 `healthy/adopted_existing` 且 0
  控制动作。完整证据见
  [`docs/evidence/issue-528-frozen-grid-recovery-cloud-release-2026-08-03.md`](docs/evidence/issue-528-frozen-grid-recovery-cloud-release-2026-08-03.md)。
- Dashboard 并发防护已部署到 Cloud Paper：当前 source 为
  `main@7b37c116513a837f69c83344548a07c1360fe7f7`，tree
  `b554dc9552836602a523ce0ebf460cb91aa2be59`。内核历史证据确认旧
  Dashboard 进程在 1.6 GiB 主机上增长到 `1,119,512 KiB` anonymous RSS
  后被 OOM kill，并继发 cloudflared/SSH 不可用；不是行情、订单或
  Supervisor 控制动作造成。#520/#522 以无 TTL 的 keyed single-flight、4 个
  worker 上限及控制前后 generation fence 修复该故障链。云端 4 个并发
  read-model 请求均在 17.70–17.73s 返回相同 HTTP 200，线程从 2 升至 6 后
  回落至 2，RSS 峰值约 220 MiB 后回落；公网为正常 Cloudflare Access 302，
  不再是 530/1033。发布前后 Paper 保持当前周期计划 active、runtime stopped、
  0 委托、0 持仓、双层对账通过；未调用 start/stop/cancel。完整证据见
  [`docs/evidence/issue-520-dashboard-concurrency-cloud-release-2026-08-03.md`](docs/evidence/issue-520-dashboard-concurrency-cloud-release-2026-08-03.md)。
- Cloud Paper Supervisor 已按分阶段流程部署并启用。当前 app/live-tick
  source 为 `main@eb2408088b34318d248d15b3c52d222635d6a531`，tree
  `c1eb574d2b90051c7ce9abae046621b9d824d798`；provider readiness、preflight
  与当前 live-tick/dashboard boot gate 均为 `pass`，Paper-only=`true`、
  provider orders/production mutation/exchange credentials 均为 `false`。
- 云端自然 tick 在 2026-08-02 21:00 北京时间边界由 Supervisor 自动收敛：
  当前 `2026-08-02_NIGHT`、计划
  `strategy-plan-2026-08-02_NIGHT-2-96ed5413` v2，38/38 accepted orders，
  runtime=`running/running`，heartbeat=`ready/fresh`，执行与会计对账通过。
  首次启动只产生一条 `start_intent`；后续 tick 均为
  `healthy/adopted_existing`，无重复控制动作、无不确定结果。
- 当前五个 Cloud timer（live-tick、dead-man、24h-report、self-review、
  backup）均 `enabled/active`；datafeed、dashboard、access gateway、
  cloudflared 及 live-tick timer 均 active；read-model 与 cloud-health
  均 HTTP 200。Dashboard 当前进程 cwd 精确落在上述 release。
- cloud-health 当前为 `degraded/warning`，唯一公开 warning 是
  `daily_self_review_missing_or_incomplete`；无 critical incidents。运行率
  窗口从本次真实 Supervisor 启动开始，首个完整窗口前保持
  `insufficient`，不把历史停机区间计入本次验收。
- 本次真实运行是上线证据，不是 48 小时验收完成证据。验收仍要求连续
  48 小时保守运行率 ≥85%、三个真实周期边界（含一次 21:00）以及一次
  完整 transient 自动恢复审计链；在证据形成前不得声称完成。
- #444 backup 根因已证实为历史 root 创建的两个 `0600` 文件，不是
  `ReadWritePaths` 或 unit 漂移；已在同一 `gridmind-backup.service`
  （User/Group=`gridmind`）下修复属主并成功生成/校验
  `backup-20260731T100551Z-6346d5ae`，manifest SHA256
  `6500c382d84280dfc7fbbf59a6ba692aba264927e6189aa90a86447c04abe41e`。
  dead-man 已端到端收到 critical fail：`fail_sent`、`delivered=true`、
  HTTP 200；critical 同时包含 Supervisor structural blocker 与
  2026-07-29 权威 Paper snapshot 过期导致的 execution unknown，均保持
  fail-closed，不能把 warning 自复盘缺失当 critical。
- 激活前基线已另存为
  `/var/lib/gridmind/outputs/cloud/baseline/activation-20260731T110013Z.json`
  （措辞为“激活前已观测到，后续用于归因对比；不作因果断言。”）。
  该基线不计入本次 48 小时运行率；当前 NIGHT 周期已在同一安全路径下
  运行，历史 blocked/过期快照只保留为不可变审计事实，不重试、不回写。
- #467 已部署并在当前 Cloud Paper Supervisor 中生效：每个 Supervisor tick 先以
  append+fsync 持久化唯一 claim，所有 heartbeat/pre-intent/start/terminal
  记录绑定该 claim；同一 fresh tick 重放只返回原 observation，claim 后崩溃
  会先零控制调用收口，不能产生第二套订单。每条 observation 锚定精确 WAL
  tail 与 claim hash，并保存可增量恢复的 episode snapshot/delta。
  `RunningEvidenceV1` 由 store 写入时间并在写/读两端重算，只在完整心跳、
  周期/计划/runtime 身份、双层对账、逻辑格位 N/N 与 Grid re-arm ancestry
  全部精确时证明 running。24h/7d 运行率只累计间隔 ≤120 秒且两端证明同一
  周期/计划/格位的相邻观测；缺失、未知、跨周期与首尾未证明均计 0，控制
  审计不得外推。首个完整窗口前保持 `insufficient`。canonical read-model
  与 Dashboard Supervisor 页公开本周期完整 attempt/start/observation
  不可变历史且无控制入口。浏览器证据见
  [`docs/evidence/issue-467/issue-467-supervisor-history.png`](docs/evidence/issue-467/issue-467-supervisor-history.png)。
  本票的实现与真实 Cloud 运行证据均不替代 48 小时验收。
- #466 已接入当前 Cloud natural tick 并生效：状态驱动的 Paper Supervisor 只在
  同一自然 live-tick 完成 lifecycle、保护单、计划同步、盘中处理、账本重建
  并持久化完整 heartbeat 后执行一次收敛。唯一 `convergence.mode` 使它与
  legacy CycleDecisionCoordinator 机械互斥；当前生产配置为
  `paper_supervisor`，legacy CycleDecisionCoordinator 在该 mode 下不调用。
  Supervisor 只调用公开推荐/计划锁/
  `prepare_start`/`start`，执行器只读 snapshot/reconcile；没有私有下单路径。
  `prepare_start` 现在内容寻址地绑定 start 前/后计划身份与精确订单
  fingerprints；#464 lease 会在公开 start 前 append+fsync intent。干净拒绝
  只有在旧计划仍精确、0 委托/持仓、双层对账与唯一 rejected 控制审计一致时
  才进入 #465 backoff；unknown/partial 仍 structural。缺失 rollover 事件、
  active-plan/stopped、market-moved 新身份恢复、并发 lease、结构性清障后恢复、
  N/N 收养与完整心跳 fail-closed 均已有集成回归。accepted order ID 必须
  非空唯一并逐项绑定 Nautilus 权威 command；活跃 re-arm 还必须具备完整
  lifecycle ancestry，持仓必须以唯一 position/trade/fill ID、计划版本、
  方向、command/order/fill 数量与精确成交加权入场价绑定原 entry command；
  非 entry 命令不能
  污染启动身份集合。跨周期持仓仍按 #413 禁区 fail-closed，不在本 PR 收养。
  fresh/missing heartbeat 与 pre-intent reservation 已进入同一哈希 WAL，
  checkpoint 按 sequence 重放，尾部截断 fail-closed；崩溃产生的 orphan
  prepared token 无法调用 start。45s soft deadline 之外另有独立 52s
  process watchdog，覆盖控制清理、恢复、fsync 与 lease release。云端 provider
  readiness、health/read-model/dead-man 已按当前 release 验证；仍不构成
  48 小时验收。
  可复验矩阵见
  [`docs/evidence/issue-466-paper-supervisor-integration-2026-07-31.md`](docs/evidence/issue-466-paper-supervisor-integration-2026-07-31.md)。
- #461 已实测并选择拓扑，当前沿用同一 Cloud host、owner
  epoch 3、clean `742c49f` 的 1,281 个自然成功 tick 跨过
  `DAY→NIGHT→DAY` 两个真实北京时间边界。基础 tick 工作 p99=1.785s、
  max=2.610s，在 55s unit 超时与 45s Supervisor 预算之间仍分别保留
  8.215s/7.390s 余量，因此 #466 使用现有 live-tick 同进程收敛，不新增
  timer，并把 AI 子预算从 30s 压到 25s；完整 tick 心跳仍必须先成功持久化。
  原始输入哈希、分阶段分位数和 unit 合同见
  [`docs/evidence/issue-461-cloud-live-tick-latency-2026-07-31.md`](docs/evidence/issue-461-cloud-live-tick-latency-2026-07-31.md)。
- #465 已接入当前 Cloud Paper Supervisor：纯状态机以精确白名单
  区分 transient/structural；五次短退避失败后告警并转为每 30 分钟持续
  probe，不把预算耗尽变成周期终止。未知/部分执行结果使用每周期 cap=2 的
  危险预算；只有 runtime、权威订单快照、控制审计共同证明零订单的拒绝才
  使用独立 cap=48（40 时 warning）的观察预算。心跳连续缺失超过十分钟才
  升级 `execution_tick_scheduler_down`；结构性解除只恢复此前观察调度，
  不重放命令。新周期预算完全重置。当前 NIGHT 已在同一 lease 下完成一次
  正常 `prepare_start → start`，随后只读收敛为 `adopted_existing`，未创建第二套订单。
- #464 已接入当前 Cloud Paper Supervisor：现在有独立的 append+fsync 哈希事件链、原子状态投影和稳定 inode 的非阻塞单飞锁；任何 `prepared_start_id` 在 `start_intent` 落盘后永久作废。响应丢失只在 active plan、runtime、精确订单 fingerprints、双层对账与 append-only 控制审计全部一致时收口为 `executed` 或可分类的零订单干净拒绝，其余一律 `control_outcome_unknown`，恢复过程零控制调用。
- #476 将 full-schedule 测试所断言的 attended Paper Nautilus runtime 改为显式 fixture，并新增缺少 runtime path 时不得凭空生成授权环境的反例；这是测试基线修复，不改变 scheduler、unit、云端运行状态或任何交易路径。
- #462 已部署并绑定当前 Cloud Paper Supervisor：AI 授权只能嵌套在 Park 通过
  Cloudflare Access 显式签发并不可变持久化的外层策略内；Gateway、
  Dashboard 与策略写入边界逐层复验签名 assertion，本机伪造邮箱/transport
  字段不能取得 Park 权限。Supervisor 只绑定精确 policy/binding
  id、version、digest，并在生产计划落盘前先持久化 AI 候选信封及逐字段比对；
  推荐上下文只读 active/latest plan，provider 失败不会把旧 proposal 晋升为
  active plan。scheduler 的 prepare/start 必须携带同一信封，prepared receipt
  也把该 ID 纳入内容寻址；A→B 替换在订单前拒绝。Grid 锁计划前先核验 proposal
  digest 与当前外层 binding，DCA 则复用页面既有 `dca-smart-fill-v1` 并冻结原始
  参数、完整 preview digest 与执行几何，仍停在人工风险确认。Park policy /
  binding 的授权时间只取服务器时钟。缺失、过期、篡改、坏行、重复身份、方向
  冲突或越界均在订单前 fail-closed。此实现未触碰任何 live/真钱路径，也未放宽
  现有行情、心跳、对账、人工确认或成交守卫。
- Cloud Paper 当前部署为
  `main@eb2408088b34318d248d15b3c52d222635d6a531`（#512，含 stale
  `plan_identity_conflict` 的只读结构性重检）。24h-report、dead-man、
  live-tick、self-review、backup 五个规范 timer 均 enabled + active；
  dead-man 近次自然投递为 `fail_sent severity=normal configured=true`，
  systemd exit=0。daily-self-review 当前按 #468 分级为 warning，不驱动
  dead-man critical；历史缺失证据仍不可伪造或回写。
- 2026-08-02 NIGHT 的 AI Grid 计划已完整走过风险闸与 Paper 执行路径：
  `strategy-plan-2026-08-02_NIGHT-2-96ed5413` v2，38/38 委托 accepted、
  38 条 lifecycle armed，当前尚无 fills/positions，execution reconciliation=`ok`、
  canonical accounting=`pass`。accepted 委托不得冒充成交证据。
- #416/#417 已修复生产执行到派生日账本的长期断链：`2026-07-28_DAY` 现为 3 trades / 6 fills / `-7.47319148 USD`，来源为验证通过的终态 StrategyCyclePackage；原始 fills/trades/周期包未改写。NAV 与基于账本计数的复盘/推广聚合必须使用重建后的派生账本，既有终态复盘和 Shadow 原始证据保持不可变。
- #406/#418、#407/#419、#420/#421 与 #422/#423 已部署：Cloud dead-man 使用当前 Paper 权威执行快照且未知仍 fail-closed；正式 Dashboard 诊断和策略控制台均返回 200；公网探针正确区分 Cloudflare Access 登录页；Linux Cloud preflight 对 29 个活动运行时文件执行 macOS 路径门禁且零违规。#424/#425 进一步把晚到行情保留为 `late_ignored`，防止重放回写既有 fill；当前 fill 时间仍保持 `2026-07-29T02:41:00Z`。
- Cloud soak 尚未终态通过：当前 cloud-health 因历史 daily self-review 不完整显示
  `degraded/warning`，不是行情、tick、执行或对账 critical；首个完整 24h 窗口前
  运行率保持 `insufficient`，不得把部署前停机区间计入新验收，也不得降低完整
  自复盘与 transient 自动恢复门槛。完整任务验收仍见
  [`docs/evidence/cloud-paper-recovery-2026-07-29.md`](docs/evidence/cloud-paper-recovery-2026-07-29.md)。
- 默认测试基线已与已合并合同重新对齐（#401）：Completion Audit 的 full/focus 调度标签包含 24 小时报表，Standard K-line 空十字线标签不再占位，Cloud 备份/预检的只读 SQLite seam 被精确登记；手工 Paper 订单仍由服务端风险事实裁决，最大计划损失保持 advisory，杠杆超限等硬闸保持 fail-closed。
- Cloud M6a 已实现：部署 manifest 锁定 trading-system/datafeed 的精确 SHA 且不携带密钥或激活 scheduler；切换前机械要求 Paper stopped、0 已接受委托、0 开放持仓、execution reconciliation 通过、备份验证、Cloud preflight 同 SHA 通过且 Cloud tick 禁用。正向切换只能 `local active -> paused -> cloud active`，失败后双端 tick 保持禁用；rollback 只接受更高 epoch 的 paused 状态再恢复 local owner。
- Cloud M5 已实现：公网链路固定为 `Cloudflare Access -> loopback 8766 allowlist gateway -> loopback 8765 Dashboard`，8100/8765 不公开暴露；gateway 只转发精确页面/API，控制请求必须通过 Access JWT 与操作者邮箱校验。`/api/trading-system/cloud-health` 分开报告行情、live-tick、执行、对账、每日复盘、备份、scheduler owner 与部署 SHA，Dashboard 生产状态卡显示 Cloud 7×24 总结；dead-man 的持久化回执不再包含 URL/token。
- Cloud M4 已实现：`outputs/` 与 datafeed SQLite 可生成自哈希、逐文件校验的离线恢复包；恢复只允许空目标、不会激活 scheduler，并强制后续 reconciliation。Paper scheduler owner 具有单调 epoch，只能 `active -> paused -> active`；一旦 cloud owner 生效，本机默认 `local-mac` 会在构造 live-tick runner 前被拒绝。
- Cloud M3 已实现：每天 01:10（北京）在终态 24 小时报表之后生成不可变 JSON/Markdown 自复盘，分开记录 tick 连续性、执行/对账、StrategyPlan/lifecycle，并明确“做对/做错/明天行动”。缺失或哈希错误证据保持 unknown；所有行动均 `executed=false`，稳定 API 为 `/api/trading-system/daily-self-review`。
- Cloud M2 已实现：systemd 可分别托管 loopback datafeed、Dashboard、one-shot live-tick、24 小时报表和 dead-man；Cloud 模式下 Dashboard/live-tick 只有在 preflight 与当前 clean SHA/tree 精确匹配时才启动。本阶段 installer 默认为 passive，只启动 datafeed，绝不激活 tick scheduler；卸载不触碰 `/var/lib/gridmind` 或 env。
- Cloud M1 已实现：Linux Paper preflight 会在零控制动作下验证 clean SHA、Paper-only 标志、持久化目录、loopback 端口、datafeed/storage、Binance USD-M 最新/历史可信 K 线和独立 Nautilus runtime；任一失败均落明确 blocked receipt。云端 app/datafeed/Nautilus 三套隔离运行环境与非密钥路径合同见 [`deploy/cloud/`](deploy/cloud/)。
- 进度: [实施进度页](docs/plans/implementation-progress-2026-07-24.md) 的 26 个已审核 story 已全部验证完成（**26/26，100%**）。M6-03 将 main SHA、发布前兼容性闸、健康/行情/执行分离、浏览器验收、证据落点及 Paper-only rollback 固化为 [release runbook](docs/runbooks/paper-release-rollback-v1.md)（[PR #365](https://github.com/zinan92/trading-system/pull/365)）。计划完成不等于自动启动或真实交易：后续每次 Paper 发布仍须按 runbook 的实时安全闸与证据步骤执行。
- GitHub provenance: `main` 的 #223–#328 merge commits 仍完整，但对应 Issue/PR 元数据对象会返回 404；这是 GitHub 元数据缺口而非代码丢失。可访问的追踪入口为 [#330](https://github.com/zinan92/trading-system/issues/330)，完整 commit 索引与“先 API 读回再报告链接”规则见 [`docs/audits/github-provenance-222-329.md`](docs/audits/github-provenance-222-329.md)。
- 架构:19 节 ports-and-adapters 重构已落地;DualTrack / Nautilus Paper 是权威验证场;live/真钱路径仍关闭。
- Grid 全链路在 main:预览/风险确认/启动/循环重挂/收口;Grid 与 DCA 启动均要求 180 秒内完整 tick 心跳,旧周期未收口一律拒绝新启动。#413 允许 12 小时边界在新周期已有显式接管 StrategyPlan、tick/行情新鲜且 Nautilus 订单/仓位/生命周期身份逐项不变时只关账不平仓；任一条件缺失仍撤单、平仓、对账并等待新启动。tick 失败现会明确标记为行情/路由、生命周期、账本写入或调度器启动阶段并给出下一步；失败绝不写心跳，运行中失联显示「运行降级」。
- DCA v1 就绪:做多/做空加仓、单张整轮 TP 世代随累计持仓更新(由行情事件触发,不是 entry 挂单,页面已标注)、独立整轮止损、风险确认、read-model 可见;聚合 TP 合同已覆盖 1/2/3 次加仓及提交失败 fail-closed。一个逻辑整轮 TP 在 Nautilus 执行层会按精确 `position_id` 拆成多张 reduce-only 子单，绝不再用共享 round ID 模糊平仓；TP/SL 后停止,v1 显式拒绝 `loop_enabled=true`。此前首次 attended 尝试在两笔加仓后暴露该执行缺陷，已安全撤单平仓，**不计作真实生命周期验收**。
- #226 attended Paper DCA 已自然闭环:做多计划 `strategy-plan-2026-07-24_DAY-5-c3bf366f` 经真实 tick 完成两次加仓；第一笔后 generation-1，第二笔后 generation-2 将聚合 TP 扩至 `0.004 @ 4037.5`。generation-2 于 `2026-07-24T06:20:00Z` 自然成交，lifecycle 为 `target_closed`、0 持仓/0 委托，execution reconciliation=`ok`，canonical accounting=`pass`（两条既有时间异常仍 quarantine）。未注入行情、人工平仓或重启执行器制造证据。
- 可观测性:请求未达后端/Cloudflare 530/Dashboard 5xx/Binance 上游/网格回滚五类故障链分立文案各带下一步;硬输入 blocker 结构化解释;历史 NAV 仅计 machine 生产已实现 P&L,缺失即显示不可用;终态周期自动尝试 production+notional-half What-if Shadow,缺失原因显式留档。
- M1 安全恢复链路完成:浏览器响应丢失后只读取 append-only 控制审计回执、权威 runtime 与活动计划身份来确认结果；`prepare_start` 和 `replace_grid` 不再自动二次请求。运行状态卡显示最近控制回执；证据不足时保留未确认状态，不猜测、也不重放控制动作。
- M2-04 聚合 TP 合同完成（#254 / `main@5d5b63b`）:第二次加仓后，终态成交必须引用最新 generation、排除已撤换 generation，并精确平掉累计数量；有效 DCA 几何的目标/止损结果互斥。该证据是纯回放，不替代真实 Paper 成交。
- #257 历史 DCA 聚合对账已部署（#258 / `main@29227f7`）:仅在 lifecycle、Nautilus 子仓位和精确 child command 三者完整对应时，read-model 才把已完成 DCA 子单重建为逻辑整轮；原始历史文件不改。部署后 reconciliation=`pass`、活动差异=0；两条早于开仓的不可变手工历史继续可见于 quarantine，不被隐藏或当作可交易状态。
- #283 canonical preflight 对账已部署（#284 / `main@6d7fea1`）:Grid 启动前风险与生产历史现在共享同一套、证据门控的 Nautilus DCA 聚合规则。当前 Paper 原始快照会误报 22 条历史 DCA 对账问题；聚合后 accounting=`pass`、engine reconciliation=`ok`、0 持仓/0 入场挂单。缺 lifecycle、子仓或精确 TP child-command 证据时，快照保持原样且仍 fail-closed。
- #280 Grid 生命周期证据包已部署（#287 / `main@d611873`）:每条 Nautilus Paper 网格线只有在 entry/TP/原价重挂、计划版本、订单/成交/交易 ID、生命周期转换和 reconciliation 都一致时才显示为「已证实」；任何缺失都显示「未验证」，不会用 K 线穿越推断成交。
- #289 Grid Paper 实证已完成（#297 / `main@5df6046`）:StrategyPlan v7 的一条 Nautilus Paper 网格线已自然完成 `entry → TP → 原价重挂`，证据包为 `completed_rearmed_count=1`、`unverified_count=0`、reconciliation=`ok`；命令、成交、快照与生命周期文件哈希见 `docs/evidence/issue-289-grid-rearm-2026-07-24.md`。之后 tick 未保持新鲜窗口，按 fail-safe 正常停止并撤掉第二代挂单；当前 Paper=`stopped`、0 已接受委托、0 开放持仓。
- #301 Grid Shadows 已扩展（#302 / `main@e9f3591`）:每个符合条件的终态 Grid 周期会在隔离 Nautilus replay 中生成生产基准、50%/150% 名义、交替偶/奇稀疏网格共 5 个同窗口 What-if；它们共享执行/费用合同与输入哈希规则，永不写生产账本、改 StrategyPlan 或创建真实订单。
- #305 Shadow 推广证据闸已部署（#306 / `main@a05ba05`）:Grid Shadow 候选只有在至少 100 笔有效可比的已平仓交易、至少两个完整周期、同窗口与执行/费用合同一致、收益优于基准且回撤/成本不恶化时才会标记 `proposal_ready`；该状态只供人工审阅，绝不自动升级主策略或下单。
- #309 Shadow 推广提案已部署（#310 / `main@db36902`）:Dashboard 现将已封包闭环周期的 Grid Shadow 证据按跨周期门槛投影为只读建议，展示支持/反证、证据 ID、窗口、执行/费用合同及收益/回撤/成本变化；同周期 What-if 不会伪装成推广结论。即便 `proposal_ready`，仍必须人工审阅并另建计划变更，系统没有自动升级或下单路径。
- #313 安全修复队列已部署（#314 / `main@f90814c`）:服务/缓存/read-model 的恢复只能进入带诊断、前后证据和验证回执的候选队列；本阶段没有执行器。订单、持仓、风险、StrategyPlan、执行引擎、行情源一律为 `requires_human_confirmation`，read-model 仅展示证据且 `command_authority=false`。
- #317 参数草稿状态矩阵已审计（#318 / `main@84a22f8`）:Grid/DCA 的 AUTO/手动、字段非法、风险确认、智能填充、DCA 参数联动及「不发控制请求」边界均经 52 项浏览器/静态矩阵复验。发现的三项失败均是测试期待旧措辞；已对齐当前更具体的账户对账、恢复下一步及正整数说明，不涉及产品、控制面或运行时部署。
- #321 Range 拖动合同已复验（#322 / `main@874ebad`）:启动前和运行中 Grid 的中间/上下边界拖动、草稿保留、取消、只读预览、风险确认与最终替换均由浏览器和控制面合同覆盖。补充真实 wheel 缩放后草稿边界仍按价格轴重绘、零控制请求的 Playwright 证据；本次仅新增回归测试，无运行时部署。
- #325 启停结果合同已复验（#326 / `main@2129b13`）:启动成功、启动拒绝与居中失败弹窗、响应中断后的未知状态，以及停止响应丢失后由权威 runtime 核对成功的路径均有浏览器覆盖。任何缺少回执的控制动作不会重发；只在运行状态、委托和持仓共同证明后才展示成功。本次仅新增回归测试，无运行时部署。
- #332 图表回归审计已完成（#333 / `main@ce67229`）:240 根仅为初始历史页，滚动/手势可在保留当前快照的同时增量加载更早可信 K 线；十字线日期在底部时间轴；策略几何变化会重新计算价格轴并保持可见网格边界对齐。远离当前行情的网格线不会强行压缩 K 线视图。本次仅新增浏览器证据，无运行时部署。
- 启动前草稿:手动参数若为空或格式无效，页面会标出具体字段、给出修复动作、清除旧预览并禁用启动；`手动` 可一键回到 `AUTO` 求解。该前端提示不放宽任何后端或 Paper 风控门禁。
- 执行测试:保护性 sweep 只处理已收盘、可信 K 线；形成中的当前 K 线不会送入执行器。#240 已恢复这一合同的锁内正反向回归覆盖。
- M1-01 运行状态合同已固化为 [`docs/contracts/authoritative-runtime-state-v1.md`](docs/contracts/authoritative-runtime-state-v1.md)：Dashboard 只消费权威 read-model；当前/上一周期、执行快照、tick、对账和不确定计数的字段所有权、降级语义与后续 fixture 矩阵已明确。下一步据此拆实现票，不在设计票中改变执行行为。
- 治理:decision-log 与 main 对账一致(近期功能 PR 完工义务全履行);pre-live 四项历史风险已复验,唯一残留 gate = naked-position 的 mainnet attended canary(docs/audits/);AGENTS.md 已仓内化;GitHub 缺号 #52–#128 有 provenance 索引。

## 下一步
- No operator action is required for the public viewer.  Use
  `https://goldbot.park-ai-intel.com/` for read-only Paper facts and Telegram
  for every strategy instruction or confirmation.  Keep the local Mac,
  Dashboard/Gateway launchd jobs, and `topic-workbench` Tunnel available; an
  unavailable local owner should make the viewer unavailable, never enable a
  fallback control or legacy Dashboard path.
- Park Telegram account admission, Neutral Grid, and bounded confirmation UX
  are merged through `main@eeb130220185f412be25663f6e6b2860c1e2e7b6`.
  The local source-bound `com.wendy.trading-orchestrator.park-paper-control`
  launchd job must be re-verified against this exact source after the local
  checkout is updated; verification remains Paper-only and read-only.
- Park can now describe a strategy naturally in Jessie Telegram; the Bot will
  acknowledge its interpretation, ask only for genuinely missing or ambiguous
  facts, and return the deterministic bilateral Paper risk plan before bounded
  Park confirmation.  Neutral Grid is now a first-class Paper option; it remains
  subject to the same trusted-market, equity, safety-gate and confirmation
  requirements as long/short.
- Park's next action is to send one complete strategy through the Jessie
  Telegram bot.  The worker will return the normalized plan, risk/maximum-loss
  and leverage calculation, then wait for `确认当前计划` or the exact digest
  confirmation before any Paper mutation.  Incomplete, duplicate, stale, or
  unconfirmed input remains blocked and cannot create orders.
- Deploy exact clean `main@2a0f6d6c8b8f79344d02fa1977aafcfc1f4d6ead` through the
  existing Paper release/boot/SHA gates. Verify the next-cycle provider
  timeout path is non-blocking, its receipt contains bounded diagnostics, and
  the next natural boundary reaches running via deterministic rebuild when no
  valid enhancement artifact exists. Do not claim #642 deployed until those
  cloud receipts exist.
- Wait for the natural `gridmind-next-cycle-plan.service` due path after
  `main@16c1909` to create exactly one immutable `verified_waiting` artifact
  with `provider_call_count=1`, compact-context observability, zero
  controls/orders/positions, and no OOM.  Do not claim #636/#639 resolved
  until that cloud evidence exists; the subsequent natural boundary must
  still prove adopted staged identity and `running_proven`.
- Cloud Paper now runs exact `main@9856f83` with #611/#612/#617 OOM
  containment, streaming hashes, calibrated datafeed bound, external failure
  alerts and both isolated canaries verified.  Evidence is in
  `docs/evidence/cloud-oom-containment-release-2026-08-11.md`.  Observe the
  next natural 09:10 self-review and 09:30 backup before claiming their
  scheduled-service acceptance; datafeed health latency is tracked separately
  in `zinan92/datafeed#6` and must not be hidden by relaxing preflight.
- Merge #605 after its scoped recovery/lineage/provenance gates, deploy the
  exact resulting `main` through the existing Paper release runbook, and wait
  for the next natural Supervisor retry. Acceptance requires a sealed
  current-cycle `running_proven`, exact SHA/boot ownership, positive planned
  order count matching the accepted authoritative snapshot, reconciliation
  pass, zero operator strategy controls, and a readable transition gap. Do not
  manually start/stop/cancel/flatten to manufacture recovery evidence. The
  separate daily-report permission failure remains outside #605.
- Keep the canonical provider-readiness timer enabled and let its natural
  five-minute polls provide failure/recovery evidence; do not manually refresh
  the proof or substitute `readiness_last_success.json` for current authority.
- 继续从当前精确 release 进行 48h Supervisor soak；只认保守运行率、三个真实
  周期边界（含 21:00）与真实 transient 自动恢复审计链，不以测试、部署或
  read-model 200 冒充终验。第一个完整 24h 窗口形成前保持 `insufficient`。
- 为显式完整 Supervisor 历史另开有界读取 Story：保留原始不可变历史与完整
  哈希链，避免当前 26.745s / 19.0MB 响应随每周期 observation 增长再次越过
  公网信封；不得把该性能工作混入交易控制、Supervisor 分类或安全闸。
- 完成 #550 的 review/merge 与精确 SHA Cloud Paper 发布；发布后只认五个连续
  自然 tick 的 timing receipt，要求 p95 <10 秒、max <20 秒、控制动作=0、
  runtime/reconciliation 持续 resolved，并观察 dead-man 自然投递。不得手动触发
  live tick、dead-man 或任何 start/stop/cancel 来制造验收证据。
- Cloud provider readiness 已由服务器上的 Paper-only Codex runtime 通过，
  不依赖 Park 的 Mac 开机；继续保留当前 source/provider SHA 绑定与 boot gate，
  不把任何密钥写入 Git、日志或报告。
- #468 的健康分级已在当前 release 生效：运行率首个完整 24h 前为
  `insufficient`，低于 85% 只 warning；structural、超过 300 秒无观察/尝试、
  episode exhausted 才 critical。daily self-review 缺失同样是 warning。
- 继续 Cloud soak。唯一完成证据仍为连续 48 小时保守运行率 ≥85%、三个真实
  周期边界（含一次 21:00）且有一次完整真实 transient 自动恢复审计链；合并、
  部署、read-model 200 或测试全绿均不等于完成。
- #455：历史 `2026-07-28_NIGHT` 周期包缺失继续按不可变 provenance 跟踪；不得伪造、
  重写或补造历史 fills/trades/周期包。它不改变当前 NIGHT 的安全运行状态，
  也不允许用猜测性重试掩盖历史事实。
- 只读监测当前 `strategy-plan-2026-08-02_NIGHT-2-96ed5413` 的 TP/SL、循环
  生命周期、tick、行情与双层对账；不得重复启动、停止、撤单、平仓或修改
  StrategyPlan。当前 38 个 accepted/armed 委托不冒充成交，后续 fills 只认
  Nautilus 权威执行与对账证据。
- 继续 Cloud soak 至首个完整北京自然日闭环，再连同 tick coverage、tick failures、
  备份、dead-man、服务、行情、owner epoch 与 Mac jobs unloaded 做终态验收；任何
  unknown 都不算通过，不启用 Mac failback。
- 监测自然 tick 单次耗时和 `late_ignored` 数量；如果再发生 timeout、不可变成交回归或执行/会计对账漂移，按独立缺陷 Issue fail-closed 处理，不得重放控制动作。
- #440 完成 #415 的执行终态返工：`plan active` 不再等于决策完成；每个周期须在 5 分钟内进入 `executed / blocked / adopted_existing`。已有 AI 计划直接走正常 `prepare_start → 风险闸 → start`，不重复跑 AI；成功必须正数 N/N 委托且 runtime=`running/running`，其余情况留下机器码、原因与下一步且同周期不重试。真钱/live 仍需 Park 本人 `park-approved`。
- #442 补齐自动周期阻塞的操作者说明：机器码与人类原因/下一步分离；已知安全拒绝给出具体 no-replay 动作，未知异常不把原始详情投影到 read-model。2026-07-29 NIGHT 的既有 blocked 决策保持不可变，未发生第二次 start。
- #445 为不可变旧回执增加只读兼容投影：裸机器码在 read-model 中派生当前人类说明并标记 `guidance_derived=true`，原始文件、decision ID 与控制历史不改写。
- M1-02:补齐 tick 剩余路由/账本失败阶段的诊断与恢复动作证据；不重做现有 180 秒心跳闸，不放宽任何 Paper 启动保护。
- M3-05:审计当前生产策略摘要与持仓/委托/成交表的字段、计数、对齐和桌面可读性；只补复现的完整性或可理解性缺口。
- M1 安全恢复:继续验证 read-model 在浏览器轮询下的完成率；#276 已隔离并压缩重证据，若再出现超时，按阶段记录原因与回执，在有证据前不自动重试任何控制动作。
- 继续积累 Grid 开仓→止盈→原价重挂与 P&L reconciliation 实绩,DCA 与 Grid 必须保持独立 StrategyPlan 与生命周期账本。
- 用 12 小时复盘与 Strategy Shadows 比较网格变体,只在足够交易样本和可持续原因成立后升级主策略。
- 实绩达标后再定义 live 准入标准;任何真钱动作仍需 Park 本人 `park-approved`。

## 实施规划（已审核）

完整的 expectation、当前 65% 基线、Milestone/Epic/Story 合同和审核顺序见 [`docs/plans/implementation-plan-2026-07-24.md`](docs/plans/implementation-plan-2026-07-24.md)。已批准按文档顺序分阶段执行；attended Paper 已获 Park 授权但每次仍须通过实时安全 preflight，真钱仍需独立人工授权。

## Appendix — 历史记录(只追加,原文搬运,不删除)

### 2026-07-29 逐票记录
- #445: 已有 blocked 周期回执若只保存裸机器码，read-model 会只读派生同一套操作者原因和 no-replay 动作；持久化 JSON 保持逐字节不变。
- #442: `prepared_start_market_moved` 等周期启动安全码现映射为明确的人类原因和下一步；未知异常只公开类型与稳定码，避免把潜在敏感原始详情带入 read-model。当前 NIGHT 决策与执行历史均未改写。
- #440: 周期决策的完成态改为“已执行、明确阻塞或接管既有运行策略”；Cloud health 按当前时钟周期检测 active 计划超过 5 分钟仍 stopped 的中间态，canonical read-model 公开终态回执。AI provider 在 live-tick 内限时 30 秒，避免 systemd 先杀进程而来不及留下 blocked receipt。
- #415: 每个 DAY/NIGHT Paper 周期现在必须有且仅有一条不可变决策；完整 live tick 后若无人工决策会生成新 AI 建议并走正常 `prepare_start → start`。人工风险确认不得自动代签；已有持仓与新方向冲突时只按精确 ID 撤掉待成交入场单，保护单身份必须保持不变，不平仓、不反手、不对冲、不创建新单，并等待自然退出。Cloud health 会将缺失或重复决策标为 blocked。
- #413: 12 小时周期边界在且仅在当前周期 active StrategyPlan 显式引用旧计划、执行 tick/行情新鲜、Nautilus 新命名空间对账通过且 accepted order/open position/Grid lifecycle ID 全部不变时执行 Paper handoff；旧周期以 `terminal_mode=handed_off` 封包，realized 留旧账、unrealized 随仓位进新账。任何缺项保留原安全撤单/平仓路径并记录确切原因。
- #433: Dashboard 生产运行状态新增一行 `策略运行占比 24h / 7d`；只按 accepted control event 中可证明的 `actual_state=running` 区间计时，进程在线、rejected 动作与无法证明的窗口前段均不冒充策略运行，证据不足显示 `--`。
- #431: Mac Paper 隔离 receipt 兼容当前 macOS `launchctl print-disabled` 的 `enabled/disabled` 输出及旧式 `true/false`；缺 label 或未知值仍 fail-closed，不会把命令成功冒充成验证成功。
- #428: Cloud owner 切换后，Mac Paper 的五个 focus launchd job 现在同时执行持久 `disable` 与当前会话 `bootout`；重启/重新登录不会自动加载。只有 owner 已明确回到 active `local-mac` 且给出专用确认词时，才会按同一 allowlist 恢复；每个 label 的前后 loaded/disabled 状态均留 receipt。
- #424: Nautilus Paper 对迟到 K 线采用“保留原始事件、追加 `late_ignored` 处置、不得回写既有成交”的执行水位合同；不可变成交检测未放宽。根因与字段级证据见 [`docs/evidence/issue-424-late-market-event-replay.md`](docs/evidence/issue-424-late-market-event-replay.md)。
- #422: 全仓环境硬编码已完成分类审计；Cloud Paper preflight 新增可执行 Linux 运行闭包门禁，生产默认值不再依赖 Homebrew、个人 macOS 主目录或固定系统 Python。launchd/failback 保留为隔离的 Mac adapter，完整清单见 [`docs/audits/environment-hardcoding-2026-07-29.md`](docs/audits/environment-hardcoding-2026-07-29.md)。
- #416: 生产 Paper 日账本改从验证通过的终态周期包投影执行事实；根因、下游影响与重建边界见 [`docs/evidence/issue-416-daily-ledger-root-cause.md`](docs/evidence/issue-416-daily-ledger-root-cause.md)。
- #406: Cloud dead-man 的仓位严重级别改读 cloud-primary 当前周期的 Nautilus 权威执行快照；仅新鲜、身份一致且执行/会计双重对账通过的空仓显示 normal，其余未知仍 fail-closed 为 critical。非 Cloud 与 live/真钱读取路径保持不变。
- #407: 正式 `/api/dashboard` 诊断在独立 datafeed 模式下不再把 Cloud 的兼容 SQLite 环境路径误组装为 legacy 市场源；full/trader/ops/strategy 合同继续使用可信 datafeed，生产 legacy/synthetic 限制未放宽。
- #420: public-access-health 现在区分 Cloudflare Access 登录页与真实 Dashboard HTML；受保护路由显示 `public_access_protected` 且特征状态为未认证不可观测，不再把 Access 页缺少应用标记误报为 `public_deployment_stale`，也未给探针新增任何认证绕过。

### 2026-07-22/23 逐票记录(蒸馏于 2026-07-23,原正文条目原样保留)
- Goldbot V5 已部署代码提交 `main@890aa89`(DCA 基线 `1b5fcd9`);部署与浏览器验收期间保留原 Paper Grid 运行态,15 张已接受挂单、0 活跃持仓,未执行启动、停止、撤单或平仓。
- 网格生命周期、循环重挂、图表 Range 草稿确认、手动风险确认和自适应参数预览均已进入 main。
- Dashboard 已修复市价单 `NaN` 导致的整页读取失败;AI 决策与策略配置完整展开,生产运行状态独立滚动,旧“运行中调整”卡片已下线。
- Nautilus 当前周期与生产历史账本已使用同一权威引擎身份规则;已清除旧 flatten 身份造成的误报启动门禁,真实歧义仍保持 fail-closed。
- Paper 启动若被行情过期或跨越网格线安全拒绝,Dashboard 会从控制审计恢复真实原因;只允许无副作用的 `prepare_start` 重试一次,创建订单的 `start` 永不自动重试。
- 单边网格的 Range 外启动已按订单可成交性区分:做多位于上边界之上、做空位于下边界之下可等待回归;反向暴露侧和中性网格仍 fail-closed。停机前也可在图上调整待启动 Range,启动失败原因改为屏幕中央展示。
- 新周期尚未建立 StrategyPlan 时也可根据可信行情智能填充 Range;该步骤保持只读,不会写计划、启动机器人或创建订单。
- GridMind 已区分“启动前风险提醒”和“运行故障”:当前 14.18x Paper Grid 的确认回执、预览 ID 与风险决策 ID 精确匹配且后台已接受,因此顶部正确显示绿色运行中;14.18x 超过 10x 的风险详情仍保留。浏览器复验为实时可信行情、15 张已接受委托、0 控制台错误。
- Paper DCA 已具备做多/做空加仓计划、累计仓位后单张整轮 TP 数量更新、整轮止损、风险确认、控制面与 V5 参数预览;浏览器已验收做多 7 参数联动重算及做空智能填充。首轮真实 DCA 生命周期尚未启动观察,不能把 UI/自动化测试当作成交证据。
- #192/#193 已部署：Paper tick 已连续两次成功，Grid 与 DCA 的最终启动均要求 180 秒内完整 tick。#194 正在把运行中的 tick 失联投影为 Dashboard 明确可见的降级状态。(注:#194 已于当日完成)
- #175 正在统一历史 NAV 的展示口径：仅机器生产已实现 P&L 可进入累计和 NAV；recovery replay 与缺失值均不能伪装为生产收益或 0。(注:#175 已于当日完成)
- decision-log 已与 main 对账补齐(2026-07-23):#96/#82/#126/#130/#134/#138 的设计决策、失败模式与验证证据已入档。
- DCA 审计后续已合并并部署(#164/#165/#166/#168):DCA×Grid 互斥与 TP 提交失败 fail-closed 均有回归测试;`loop_enabled=true` 在 v1 被显式拒绝,概要恒显示「完成后停止」;静态套件在修正 #154 遗留的 `actionStatus` 断言后恢复全绿。重启 Dashboard 后 Grid 运行态不变(running、15 挂单、0 持仓),页面 0 控制台错误。
- #171/#185: Nautilus Paper 启动/预启动现要求当前周期 `dualtrack-live-tick` 的 180 秒内成功心跳；仅完整完成行情、生命周期与账本刷新后才落盘，避免反复崩溃制造假绿。
- #173: weekly ledger 对 deploy-canary 等非 ISO 历史 cycle/date 记录诊断并跳过，不能再中断 Paper tick；有效日期账本仍按原周归属计算。
- #172: 跨周期 Paper runtime 不再被当前周期静默投影成“0 委托”。上一周期仍在运行或仍有已接受委托时，Dashboard 显示具体周期与数量，任何新 Grid/DCA 启动一律拒绝；rollover 只负责停止、撤单/平仓与封包，下一周期必须由操作者明确启动，绝不从旧几何自动生成计划。
- #189: tick 的生命周期顺序已改为“关闭旧周期 → 收口旧 Paper runtime → 规划当前周期”。历史行情下载超时不能再阻止旧周期的安全收口；若 rollover 本身阻塞，当前周期规划明确跳过。
- #174: 每个 terminal Paper package 现在会在隔离 Nautilus Shadow 中尝试生成 production 基准与 `notional-half` What-if；缺少事件、runtime 或 preflight 会作为明确原因留在 package/页面，Shadow 失败不会重开或阻塞生产周期收口。
- #176: canonical accounting 会保留任何“平仓早于开仓”的历史原始记录并写入明确 reconciliation 诊断；Dashboard 将其隔离为“时间异常”，不再计为正常已完成交易。
- #177: DCA lifecycle 的聚合止盈世代、累计数量和退出状态已从 Paper JSON 接入 read model；页面明确说明整轮 TP 由行情事件触发、不是 entry 挂单。
- #178: 24 小时报表已成为受控 launchd 任务，直接执行本仓 pipeline；不再依赖已移动的外部 wrapper。调度状态会给出受限 stderr 摘要、失败分类、下一步以及报表产物是否存在。
- #370: launchd 兼容性不再假定所有 Paper job 都使用 `/usr/bin/python3`。Dashboard 与 live-tick 会按各自 plist 的 `ProgramArguments`/`PATH` 解析并逐一探测；Nautilus Python 作为独立依赖验证。任何 discovery/probe 异常都会保留 blocked receipt，但机械接入 restart 路径仍由下一张发布安全票完成。
- #372: Paper 发布闸已机械接入 schedule install/rollback、attended Nautilus cutover/rollback 与旧 `start.command`：receipt 必须 pass、包含兼容性通过、15 分钟内新鲜且 Git SHA 精确匹配当前 checkout，才允许任何 bootout/bootstrap/kickstart 或前台启动；失败路径只写 blocked receipt，不执行修改命令。
- #374: release runbook 已校正 gate 的 JSON/plain 输出、`/dashboard-v5.html` 路由别名与磁盘 `dashboard-gridmind.html` 的区别，并要求 source-bound mutation receipt 才能声称部署 SHA 或 live-tick 未重启；缺证据一律标 `unknown`。26/26 页使用不可变 completion baseline，不再假装等于移动中的 HEAD。
- #376: Paper pre-deploy receipt 进一步绑定 commit tree 与 tracked checkout 清洁状态；Dashboard 和 dualtrack live-tick 在 boot 时必须验签后才能绑定端口或构造 runner。主动重启仍要求 15 分钟新鲜回执，同一已发布版本的 KeepAlive 自愈和周期 tick 不因时间流逝失效；开发改动必须在独立 worktree 完成。
- #378: GOLD 的时间边界生命周期读取从 cache-only 改为 cache-first、零行时回同一 `binance_usdm_futures` execution venue；最新行情仍保持 bypass+strict，fallback/synthetic 仍禁止。任何上游或合同失败继续不写 tick heartbeat，并冻结 Grid/DCA 新启动。
- #380: Paper source-attestation boot 拒绝使用专用退出码 79，并以 service boot receipt 为权威诊断；78 保留为标准 `EX_CONFIG`，不得再把两者混报。
- #382: launchd 若在 Python 前报 LWCR/Invalid argument，只能通过 fresh source-bound receipt 驱动的单标签 rebootstrap 恢复；命令限制为 Paper allowlist，记录前后状态，不得用 broad schedule reinstall 或裸 kickstart 掩盖变更范围。
- #180: GridMind 的 Paper 操作者主路径现在有一条连续 Playwright 验收：趋势刷新、智能填充、启动前调整、启动、停止，以及复盘/Shadows/NAV 入口均验证可见结果和控制请求；详细图表、拖动、历史加载仍由各自的聚焦浏览器测试覆盖。
- #181: `codex/recovery-stash-20260721` 的四个 handoff 指定文件已逐项三方审计；均不应直接恢复，恢复分支仍完整保留为只读证据，结论见 `docs/audits/recovery-stash-20260721.md`。
- #182: GitHub 404 的历史 #52–#128 已有不可伪造的本仓 provenance 索引；只记录可复验 commit/decision-log 证据，绝不伪造原 Issue，见 `docs/audits/missing-issue-provenance-52-128.md`。
- #183: 四项历史 pre-live 风险已重新复验：日损 NaN/时区/陈旧证据 fail-closed、测试网保护单回收、百分比仓位口径一致、Obsidian 可选；mainnet 仍被独立 activation/canary 门禁阻断，见 `docs/audits/pre-live-risk-invariants-2026-07-23.md`。
- #147: 根 `AGENTS.md` 已由失效的外部符号链接替换为仓内可读的 Paper 安全、交付和证据规则；#137 已按已合并的 #138 与浏览器回归证据关闭。
- #146: 自适应求解器的硬输入边界仍不可绕过，但不再将 2–200 格、整数格数或 1–20x 杠杆错误以裸 `ValueError` 交给操作者；控制面返回不可执行的结构化 blocker，Dashboard 显示原因和下一步。候选 Range/策略类型的不可覆盖 blocker 同样有专属说明。
- #145: GridMind 已把「请求未到后端」「Cloudflare 隧道 530/1033」「Dashboard 5xx」「Binance USD-M 行情上游」和「完整网格未被接受后安全回滚」分开说明，每种状态都包含下一步；展示没有放宽任何行情或执行 fail-closed 门禁。
- #214: 策略类型的 Grid / DCA 选择使用高对比选中卡、`✓ 当前选择` 标签和同步的 `aria-pressed` 状态；切换后仅一个类型保持选中，策略计算与下单语义未变。
- #216: 浏览器未收到 Dashboard 响应时不再断言请求未到后端或 Paper 未改变；控制动作一律进入权威 read-model 核对，读取动作明确提示刷新重试。
- #218: AI 市场评估改为位置优先：D1 200 根完整日线先给出低/中/高位与方向倾向，再由 D1/4H 明确震荡、形成中、已形成趋势，最后才推荐 Grid/DCA 与确定性参数职责；只写提案与本地预览，绝不自动启动或下单。
# 2026-07-27 Cloud M6b — passive host provisioning in progress

- Cloud M6a merged as PR #397 at `main@ec8be27`: exact-source deployment
  manifests and fail-disabled single-owner cutover/rollback are available.
- Issue #398 now owns the Alibaba Cloud Singapore passive-host build. The
  purchased host is a Simple Application Server running Ubuntu 24.04 x86_64
  with 2 vCPU, 2 GiB memory, and 40 GiB storage. The provider/login/payment
  boundary is complete; no Paper scheduler, strategy, order, position, exchange
  credential, or live process has been moved.
- Next: merge the source-bound Alibaba adapter, upload exact source archives,
  activate only loopback datafeed and Dashboard, then prove authenticated
  access and temporary restore before the separate M6c scheduler cutover.
