# D32: Expo SDK pin policy — deterministic CI, deliberate bumps

- Date: 2026-10-13, Status: accepted, Task: T0.2b (hotfix)

## Context

After T0.4 scaffolds the mobile app with Expo SDK 57 packages pinned to specific patch versions,
Expo publishes patch releases (e.g., 57.0.28, 57.0.20, 57.0.26, 57.0.6) before the next
Chronos task is scheduled. The CI job `mobile` runs `expo-doctor`, which by default fetches
Expo's **live SDK-57 manifest** and fails if any pin is behind the current patch level. This
caused `main` and every PR to fail on 2026-10-13 without any code change—violating D18/D19
(CI must be deterministic; repo content alone decides pass/fail).

The relevant expo-doctor environment variable (`EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK`) is
documented in `node_modules/expo-doctor/build/utils/env.d.ts` and has been stable since
expo-doctor 1.x.

## Decision

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D32-1 | Live-manifest version check in CI | Disable via `EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK=1` set inline in the `check-mobile` justfile recipe | The check is a **linting advisory** ("you are behind the latest patch"), not a correctness gate. It consults live registries, making CI non-deterministic. The remaining 20 expo-doctor checks (peer deps, config, module compatibility, etc.) still run. | A future expo-doctor version adds a config-file mechanism that is deterministic; switch to that. |
| D32-2 | Drift visibility | Weekly informational step in `audit.yml` runs `expo install --check \|\| true` and prints current vs. recommended versions; non-failing | Drift is never silently forgotten; the weekly audit log shows it. Developers can choose to bump pins deliberately by running `expo install --fix` and committing as part of a task plan. | The weekly advisory is consistently ignored for >4 weeks; escalate to a task-planning requirement in that event. |
| D32-3 | When Expo pins are bumped | Only as part of a deliberate task plan; never auto-bumped by Dependabot or CI workarounds | Lock-step control: every version change is reviewed in a task plan before merging, keeping the codebase stable and intentional. | A tool or circumstance makes manual bumping infeasible (e.g., a critical security patch with no patched version available except the latest). |

## Consequences

- `EXPO_DOCTOR_SKIP_DEPENDENCY_VERSION_CHECK=1` is set as a shell-prefix assignment (scoped to that
  one command) in the `check-mobile` recipe in `justfile`. This applies identically to local
  developer runs and to the CI job `mobile`, which inherits the recipe (D18).
- expo-doctor continues to run and must pass; only the live-manifest version sub-check is skipped.
  A genuine scaffold error (bad peer dep, wrong config, broken module) will still fail the doctor.
- The standalone `build` recipe does not invoke expo-doctor and needs no change.
- The weekly `audit.yml` workflow includes the "Expo SDK drift (advisory)" step after the JS audit
  informational step; it exits 0 regardless of drift.
- Expo SDK patch updates are **no longer a source of CI flakiness**. When a task plans an Expo
  upgrade, it bumps pins, runs `pnpm install`, commits the lockfile, and records the decision.
- A genuine version-range mistake (e.g., `expo@56.x` with an SDK-57 project) will still fail at
  build time (`expo export --platform ios` in `check-mobile`, or `expo prebuild` in
  `ios-build.yml`), confirming the skipped check is not the only safeguard.

## Why this is not a weakening

The skipped sub-check only answers "are your pins at Expo's current patch level?" — a linting
signal, not a correctness gate. What still catches real problems:

1. The **other 20 expo-doctor checks** validate peer dependency compatibility, module
   configuration, plugin registration, and more.
2. **`expo export --platform ios`** in `check-mobile` compiles the full JS bundle; an
   incompatible version would fail here.
3. **`expo prebuild`** in `ios-build.yml` validates native configuration; an out-of-range SDK
   version would fail here.
4. **`pnpm install --frozen-lockfile`** in CI ensures the lockfile exactly matches `package.json`;
   any unintended pin change is caught.
5. The **weekly advisory** makes drift visible in the audit log; it is never silently ignored.
