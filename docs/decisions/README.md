# Decision records

D01–D30 are in `docs/01-decisions.md`. New decisions made during tasks go here as
`Dnn-slug.md` (next free number), using the same table shape:

```
# Dnn: title
- Date, Status, Task
## Context
## Decision (table: ID | Decision | Choice | Why | Revisit if)
## Consequences
```

## Index

| ADR | Title | Task |
|---|---|---|
| [D31](D31-pnpm-audit-ignores.md) | pnpm audit ignored advisories | T0.4 (review fix) |
| [D32](D32-expo-doctor-deterministic-ci.md) | Expo SDK pin policy — deterministic CI, deliberate bumps | T0.2b |
| [D33](D33-dependabot-security-only.md) | Dependabot security-only for pip/npm; `deps` scope in pr-title | T0.2d |
| [D34](D34-device-session-keychain.md) | Device-bound session available after first unlock | T2.2 |
| [D35](D35-title-dedupe-jaccard.md) | Near-duplicate headlines by word-set Jaccard, not simhash distance | T3.3 |
