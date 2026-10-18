# D33: Dependabot security-only for pip/npm; `deps` scope in pr-title

- Date: 2026-10-13, Status: accepted, Task: T0.2d

## Context

On 2026-10-13, eleven Dependabot version-bump PRs were opened simultaneously:

- **pip** (`services/api`): SQLAlchemy 2.1.4 — incompatible with SQLModel's `<2.1` upper bound,
  risking import failures across the API.
- **npm** (`apps/mobile`): expo-dev-client 58 corresponds to the next Expo SDK (not yet adopted
  by this project); zod 4.6.6 conflicts with Remotion 4.5.4's peer-dependency range.
- **github-actions**: 9 SHA-bump PRs opened alongside the ecosystem PRs.

The root cause was `open-pull-requests-limit: 5` for pip/npm entries: GitHub interprets any
non-zero value as permission to open version-update PRs in addition to security-update PRs.
Security updates ignore the limit and are always opened.

Separately, Dependabot's `github-actions` PRs use title format `ci(deps): bump …`; `deps` was
not listed as an allowed scope in `.github/workflows/pr-title.yml`, so every Actions SHA-bump
PR was blocked by the PR-title check.

## Decision

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D33-1 | Version-bump PRs for pip/npm | Disabled via `open-pull-requests-limit: 0` | The only documented GitHub mechanism to suppress version updates while preserving security-update PRs (security PRs bypass the limit). Lockfile changes must be deliberate and task-planned (D19). | A security advisory cannot be patched without also bumping the major/minor version; re-enable version updates for that ecosystem temporarily via a task plan. |
| D33-2 | github-actions Dependabot | Weekly SHA-bump PRs allowed (`open-pull-requests-limit: 5`, not 0) | SHA pins must stay current to prevent supply-chain drift; the owner reviews and merges weekly. | Pin staleness causes CI failures that cannot be avoided any other way. |
| D33-3 | `deps` scope in pr-title | Added to `scopes` list in `pr-title.yml` | Dependabot's `commit-message.prefix` does not support nested scope syntax (`ci(ci)`), so the simplest fix is to accept the `deps` scope it generates. Rejected alternative: change prefix to `ci` — loses the semantic distinction between infrastructure CI changes and dependency updates. | Dependabot adds a config-file mechanism for commit-message scope. |

## Consequences

- pip/npm Dependabot will only open PRs for CVE-patched versions; the weekly advisory in
  `audit.yml` remains the visibility mechanism for version drift (D32-2).
- `github-actions` Dependabot continues to open weekly SHA-bump PRs under the `ci(deps)` title,
  which now passes pr-title validation.
- Any future pip/npm `open-pull-requests-limit` change must go through a task plan so the
  conflict pattern above is not re-introduced silently.

## Revisit if

- GitHub documents a dedicated `security-updates-only: true` key (currently undocumented but
  occasionally referenced in community discussions); switch to it for explicitness.
- A pip or npm security update itself depends on a breaking version bump (e.g., CVE fixed only
  in SQLAlchemy 3.x when SQLModel still pins `<3.x`); handle as an emergency task plan.
