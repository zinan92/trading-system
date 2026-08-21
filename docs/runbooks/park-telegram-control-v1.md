# Park Telegram control-plane drill (Paper-only)

This runbook covers the transport/proposal stage only.  It does not enable the
Park cutover and it cannot submit, cancel, or flatten an order.

## Required environment

Inject these values through the target Paper host's secret mechanism.  Never
put them in Git, an Issue, a receipt, or a log:

- `TRADING_ORCHESTRATOR_TELEGRAM_BOT_TOKEN`
- `TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID`
- `TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID`
- `TRADING_ORCHESTRATOR_OUTPUT_ROOT`
- `DEEPSEEK_API_KEY`

Optional provider settings are host-owned only: `DEEPSEEK_API_URL`,
`DEEPSEEK_MODEL`, `DEEPSEEK_TIMEOUT_SECONDS`, and an explicit Codex path via
`PARK_CODEX_CLI`, `CODEX_CLI`, or `TRADING_ORCHESTRATOR_CODEX_CLI`.

The Bot token and chat/user identity must be the one Park-controlled chat.

## One polling pass

```bash
python3 -m pipelines.park_telegram_control --timeout-seconds 20
```

The command persists the update cursor, inbox/outbox ledger, proposal and
delivery receipts under `outputs/park_strategy/`.  A successful Telegram send
requires the API response's explicit `result.message_id`.  Missing or failed
receipts remain failed/dead-lettered and are not treated as delivered.

## Trading Expert behavior

The bot starts as a bounded Trading Expert, not as a strategy form. It has
separate research, discussion, read-only query, strategy-forming, and
confirmation-ready modes. It may answer current Paper facts, compare trading
ideas, challenge assumptions, and politely redirect unrelated topics. Research
and discussion stay open even when some Grid/DCA fields are present.

When Park is forming an execution decision, the bot preserves bounded
conversation context and asks natural-language follow-ups. It retains explicit
Grid/DCA fields plus missing fields, evidence, assumptions, and conflicts. A
complete field set alone does not converge: Park must explicitly ask to
finalize/execute before a deterministic proposal can be created. A Conversation
Candidate or assistant reply is never execution authorization.

The deterministic normalizer, risk planner, market/freshness gates,
reconciliation, ownership checks, Paper-only gate, and exact Park confirmation
remain the only path to a proposal or execution.

## Safety interpretation

- Natural-language intent uses DeepSeek first, then the bounded Codex CLI on
  timeout/error/invalid output.  If both are unavailable, only the existing
  narrow deterministic fallback may run; no provider may authorize a plan.
- A strategy message creates a deterministic proposal only after trusted/fresh
  market, authoritative Paper equity, reconciliation and clean-slate checks.
- The proposal includes its exact digest and requires
  `confirm <digest>` (or `确认 <digest>`).  Confirmation is recorded as a
  capability and still produces zero orders in this story.
- Existing Park cutover remains `feature_enabled=false`; this command is not a
  release or runtime-readiness proof.
