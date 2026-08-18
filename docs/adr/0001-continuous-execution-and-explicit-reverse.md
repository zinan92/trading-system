# Continuous execution and explicit Reverse

Status: accepted

Park Paper execution is continuous across 12-hour Recording Windows; a window
boundary creates only a 12h Package and never performs an execution handoff,
Cancel, Flatten, Stop, or Reverse. There is no valid cycle-scoped strategy in
the product model. A Strategy Revision exists only when Park explicitly
requests a strategy change. A strategy otherwise ends only at take profit or
stop loss. For a Reverse, the system may calculate a complete candidate
specification, but Park must approve the exact proposal; after approval, the
old entry orders are cancelled, old positions are flattened and reconciled,
and only then may the new strategy start. Any implementation path that creates
or switches a plan merely because a Recording Window changed is a contract
violation.
