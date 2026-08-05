# Architecture decision records

Decisions that shape the SDK beyond a single change live here, so the reasoning
survives the pull request that carried it. A record is added when a choice is
hard to reverse, spans repositories, or has a security boundary in it — not for
routine implementation decisions, which belong in code comments and the
changelog.

Records are numbered, immutable once accepted, and superseded rather than
edited: a decision that no longer holds gets a new record that says so, and the
old one keeps its status line updated to point at it.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-distributed-trace-context.md) | Distributed trace-context propagation | Accepted, implementation gated |
