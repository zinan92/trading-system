# Domain Docs

This is a single-context repository.

## Before exploring

Read:

- `CONTEXT.md` at the repository root, if present;
- relevant ADRs under `docs/adr/`, if present.

Missing domain docs should not be treated as errors. Create them lazily when domain terms or durable decisions are resolved.

## Vocabulary

Use the glossary terms defined in `CONTEXT.md`. If a needed term is missing or overloaded, resolve it through `/domain-modeling`.

## ADR conflicts

If a proposed change conflicts with an existing ADR, surface the conflict explicitly rather than silently overriding it.
