# Security

Chronos-AI is a single-user system that runs on the owner's laptop and phone. There is no hosted
service and no user data other than the owner's own. The threat model and controls are in
`docs/09-security.md`.

## Reporting

Open a private security advisory on GitHub (Security → Advisories → "Report a vulnerability") or
email the owner. Do not open a public issue for a vulnerability.

## Incident steps (owner)

1. Lost or compromised phone: `uv run chronos device revoke` in `services/api`, then re-pair.
2. Suspected laptop compromise: rotate `CHRONOS_SECRET_KEY`, revoke the device, regenerate the
   mkcert CA (`just certs --rotate`), rotate Expo/APNs credentials, review `data/logs/`.
3. Secret committed to git: rotate it first, then rewrite history with `git filter-repo` and record
   the event in `docs/decisions/`.

## Scanning

`just security` runs semgrep, bandit, pip-audit, pnpm audit and gitleaks locally. CI runs
semgrep, bandit and gitleaks in the core jobs and zizmor and a weekly audit as advisory checks
(CodeQL and dependency review need GitHub Advanced Security, which this private repo lacks). Actions are pinned to commit SHAs and updated by Dependabot.
