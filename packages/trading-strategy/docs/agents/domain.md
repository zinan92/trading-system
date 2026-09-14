# Domain Docs

This is a single-context repository.

## Before exploring

Read:

- `CONTEXT.md` at the repo root, if present
- `docs/adr/` decisions relevant to the current work, if present

Missing domain docs should not block exploration. Create them lazily when a
domain term or durable architectural decision needs clarification.

## Layout

- Root `CONTEXT.md`
- Root `docs/adr/`

## Vocabulary

Use the terms defined in `CONTEXT.md`. If a needed concept is not defined,
record the gap before introducing a competing synonym.

## ADR conflicts

If new work contradicts an existing ADR, surface the conflict explicitly
instead of silently overriding it.
