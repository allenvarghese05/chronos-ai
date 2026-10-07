# D31: pnpm audit ignored advisories

- Date: 2026-10-06, Status: accepted, Task: T0.4 (review fix)

## Context

`pnpm audit` surfaces three advisories in `apps/mobile` that cannot be resolved by a dependency
override because no patched versions exist upstream. The advisories are suppressed via
`auditConfig.ignoreGhsas` in `apps/mobile/pnpm-workspace.yaml` so that `just security` (which
runs `pnpm audit --audit-level=high`) does not block development. All three are in build-tooling
or test-tooling; none are in the app's runtime bundle that ships to the device.

The weekly `audit.yml` workflow contains an informational step (after `security-web`) that runs
`pnpm audit --json | python3 -c '…'` to list ALL advisories including ignored ones in the job
log, so the ignored set is visible even though the job exits 0.

**Note:** `pnpm audit --audit-level=high` passes because GHSA-hp3w-g68c-fv3c is `moderate`
severity, and GHSA-86w9-cpqp-85rv / GHSA-vfj7-8cjw-p6xm are suppressed via `ignoreGhsas`.

## Decision

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D31-1 | GHSA-86w9-cpqp-85rv (`node-forge` RSA key-material bypass) | keep `ignoreGhsas` | **no patched version exists** (`node-forge <= 1.4.0`, patched: None per advisory API 2026-10-06); transitive path: `chronos-mobile → expo@57.0.27 → @expo/cli@57.0.28 → node-forge@1.4.0`; an override to any `node-forge` version would not help because no safe release exists | next Expo SDK bump or when the advisory lists a patched version |
| D31-2 | GHSA-vfj7-8cjw-p6xm (`braces` ReDoS) | keep `ignoreGhsas` | **no patched version exists** (`braces <= 3.0.3`, patched: None per advisory API 2026-10-06); transitive path: `chronos-mobile → jest@29.7.0 → @jest/core@29.7.0 → micromatch@4.0.8 → braces@3.0.3`; also reached via `react-native@0.86.3 → @expo/cli`; no upstream release to override to | next Expo SDK bump or when the advisory lists a patched version |
| D31-3 | GHSA-hp3w-g68c-fv3c (`sprintf-js` DoS) | keep `ignoreGhsas` | **no patched version exists** (`sprintf-js <= 1.1.3`, patched: None per advisory API 2026-10-06); transitive path: `chronos-mobile → jest@29.7.0 → @jest/transform@29.7.0 → babel-plugin-istanbul@6.1.1 → @istanbuljs/load-nyc-config@1.1.0 → js-yaml@3.15.2 → argparse@1.0.10 → sprintf-js@1.0.3`; no upstream release to override to | next Expo SDK bump or when the advisory lists a patched version |

## Consequences

- `just security` (`pnpm audit --audit-level=high`) continues to pass. GHSA-hp3w-g68c-fv3c
  (`moderate`) would not fail the high-level gate in any case; the other two are ignored.
- The weekly `audit.yml` informational step prints all advisories (including ignored) to the job
  log so they are not silently forgotten.
- **`pnpm audit` and the weekly audit workflow will not surface these three advisories** as
  failures while they remain in `ignoreGhsas`. They will appear in the informational log step.
- Review trigger: next Expo SDK bump, or when any advisory above lists a patched version.
