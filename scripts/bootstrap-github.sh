#!/usr/bin/env bash
# Create the GitHub repo, set merge/security settings, push main, protect it (T0.2).
# Usage: scripts/bootstrap-github.sh [repo-name] [private|public]
# Run from the repo root after `gh auth login`. Idempotent where GitHub allows it.
set -euo pipefail

REPO="${1:-chronos-ai}"
VISIBILITY="${2:-private}"

gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }
OWNER="$(gh api user --jq .login)"

# 1. Local repo with main as the default branch
[ -d .git ] || git init -b main
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  git add .
  git commit -m "chore: project plan, agents, CI and tooling skeleton"
fi

# 2. Create the GitHub repo and push (skip if it exists)
if ! gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  gh repo create "$REPO" "--$VISIBILITY" --source . --remote origin --push \
    --description "Chronos-AI: behavioural wake detection on iOS + a zero-cost programmatic morning reel"
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "git@github.com:$OWNER/$REPO.git"
  git push -u origin main
fi

# 3. Merge settings: squash only, auto-delete merged branches, no auto-merge (owner merges by hand)
gh repo edit "$OWNER/$REPO" \
  --enable-squash-merge \
  --enable-merge-commit=false \
  --enable-rebase-merge=false \
  --delete-branch-on-merge \
  --enable-auto-merge=false

# 4. Security features: secret scanning + push protection, Dependabot alerts
# (Secret scanning needs a public repo or GitHub Advanced Security; gitleaks in CI covers private repos.)
gh api --method PATCH "repos/$OWNER/$REPO" \
  --input - <<'JSON' >/dev/null 2>&1 || echo "Secret scanning not available on this repo (private without GHAS); gitleaks CI job covers it."
{ "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" } } }
JSON
gh api --method PUT "repos/$OWNER/$REPO/vulnerability-alerts" >/dev/null || true

# 5. Labels used by the pusher agent
for l in api mobile renderer ci docs ops agents M0 M1 M2 M3 M4 M5 M6; do
  gh label create "$l" --repo "$OWNER/$REPO" --force >/dev/null 2>&1 || true
done

# 6. Environment for the manual EAS workflow (secret added by hand: EXPO_TOKEN)
gh api --method PUT "repos/$OWNER/$REPO/environments/mobile-build" \
  --input - <<JSON >/dev/null || true
{ "reviewers": [ { "type": "User", "id": $(gh api user --jq .id) } ] }
JSON

# 7. Protect main: PR required, required checks api/mobile/renderer/secrets, signed commits,
#    conversation resolution, no force-push/deletion, no bypass actors.
if gh api --method POST "repos/$OWNER/$REPO/rulesets" --input "$(dirname "$0")/github-ruleset.json" >/dev/null; then
  echo "Ruleset created: main is protected."
else
  echo "Could not create the ruleset (already exists, or private repo without GitHub Pro)."
fi

echo "Done: https://github.com/$OWNER/$REPO"
echo "Next: add EXPO_TOKEN to the mobile-build environment when you set up EAS; set up commit signing (git config commit.gpgsign true)."
