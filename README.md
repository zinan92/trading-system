# trading-strategy

Independent local Strategy foundation extracted from
`/Users/wendy/work/trading-system-testnet` at source baseline
`b841800ee03fd98107063c0cbbf5144096a5c4c0`.

The package preserves the existing Canonical DCA and Grid plan/schema,
precision, geometry, Hard Stop, rung lifecycle, re-arm, and pure DCA replay
semantics. It contains no runtime dependency on a broker, Dashboard,
Telegram/Park authorization, Cloud runtime, network, credentials, or
filesystem order execution.

The composition root remains responsible for authorization, risk admission,
execution adapters, persistence, lifecycle orchestration, read models, and
control glue. In particular, `dca_execution_lifecycle.py`,
`park_dca_track.py`, and `park_grid_track.py` were audited but are not copied
into this package.

## Verification

```bash
python3 -m pytest -q
python3 -m compileall -q trading_strategy tests
```

The golden fixture in `tests/fixtures/canonical_golden.json` was generated
from the source baseline before implementation copying. The tests compare
complete selected DCA/Grid envelopes and state-transition receipts to that
fixture.
