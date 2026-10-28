# Security

## 1. Threat model

| Asset | Threats | Impact |
|---|---|---|
| Device bearer token | Sniffed on LAN; leaked in logs; stolen from a backup | Attacker can post fake telemetry, read reels, change rules |
| Laptop API on LAN | Any device on the Wi-Fi can reach it; guest network devices; compromised IoT | Unauthenticated endpoints abused; DoS; pairing brute force |
| Fetched content (feeds, article HTML, images) | Malicious URLs (SSRF to `169.254.*`, loopback, router admin), zip-bomb images, oversized downloads, HTML injection into the script | Internal network probing; disk/memory exhaustion; prompt injection into the LLM |
| LLM output | Prompt injection via feed text ("ignore instructions, say…"), hallucinated links, unsafe text | Embarrassing/incorrect reel; links to malicious sites |
| Subprocesses (ffmpeg, whisper, remotion, ollama) | Argument injection from filenames/titles; runaway processes | Code execution; laptop unusable at 6 a.m. |
| Supply chain | Malicious npm/PyPI packages; unpinned GitHub Actions; compromised model files | Code execution on laptop and CI |
| Secrets in git | `.env`, APNs key, Expo token, mkcert key | Full compromise |
| Telemetry data | Reveals daily wake times and phone usage | Privacy |

## 2. Controls

### Transport and auth
- HTTPS only (D16): `mkcert` root CA generated on the laptop (`just certs`), leaf cert for the LAN
  hostname (`chronos.local` via mDNS + the LAN IP as SAN); root CA profile installed on the phone
  (Settings → General → About → Certificate Trust). Cert and key under `data/certs/` mode 600; API
  refuses to start with HTTP outside `CHRONOS_ENV=test`.
- API binds to the LAN interface IP (configured), never `0.0.0.0`; `/metrics` to loopback.
- Bearer tokens: 256-bit random; stored as SHA-256; constant-time compare; sent only over TLS;
  stored on the phone in `expo-secure-store` (Keychain, `AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY`, per D34; unavailable until the first unlock after reboot).
- Pairing: 8-char code (40 bits), 10-minute expiry, one-time, 5 attempts then burned, 5/min per IP;
  code only ever displayed on the laptop terminal/QR. Only one active device; re-pairing revokes.
- Signed media URLs: HMAC-SHA256 over path+expiry with a server secret from `.env`; 1 h expiry;
  no directory listing; path traversal impossible (ids are ULIDs, file names from an allowlist).
- Rate limits per IP on every router; 413 above 64 KB request bodies.

### Input validation
- Pydantic `extra="forbid"` everywhere; enums for all categorical fields; numeric bounds
  (`steps_10m ≤ 5 000`, `*_sec_10m ≤ 600`); timezone must be in `zoneinfo.available_timezones()`.
- Clock skew check (≤ 5 min) rejects replayed/old events.
- `WakeRules` validated as a whole (weights ≥ 0, caps ≥ max points, thresholds ∈ [1, 100],
  window start < end, `tick_interval_min ∈ [2, 30]`).

### Outbound fetch (SSRF guard, `chronos/http.py`)
- Single `httpx.AsyncClient` with: scheme allowlist `http/https`; DNS resolution before connect
  and rejection of private, loopback, link-local, multicast, and unique-local ranges (IPv4+IPv6),
  re-checked on every redirect (max 3); 15 s timeout; `max_bytes` streaming cap; content-type
  allowlist per use (feeds: xml/json/html; images: jpeg/png/webp; video: mp4).
- Hostname allowlist for the configured sources is **not** used (og:image may be on any CDN),
  hence the IP-range guard is mandatory and tested with a fake resolver.

### Content and LLM
- Feed text is HTML-stripped and length-capped before it reaches the prompt; the prompt frames
  stories as data inside JSON, with an instruction that story text is untrusted.
- Structural defence (not just instructions): the validator rejects any story not in the
  candidate set, any URL not matching the item, word-count violations and deny-listed terms.
- LLM output is never executed, templated into shell or HTML, or used as a filename.

### Subprocesses
- One wrapper `media/subprocess.run(argv: list[str], *, timeout, cwd, env)` — no `shell=True`
  anywhere (bandit `B602/B603/B607` enforced); arguments built from internal paths only; file
  names are `scene-NN.jpg`, never titles; `cwd` under `data/`; minimal env; `timeout` kills the
  process group; stdout/stderr truncated to 64 KB in logs.
- ffmpeg invoked with `-nostdin -y -hide_banner` and explicit `-i` inputs; Remotion props passed
  via a JSON **file**, not the command line.

### Secrets
- `.env` + `data/` gitignored; `services/api/.env.example` documents every variable with a
  placeholder; `gitleaks` pre-commit hook and CI job; `git-secrets`-style regexes for Expo tokens,
  APNs keys, OpenAI/Anthropic keys.
- Settings never log secret fields (`SecretStr`); structlog processor redacts `token`, `sig`,
  `authorization`.
- GitHub: `EXPO_TOKEN` only in the manual EAS workflow environment `mobile-build` with required
  reviewers = owner; Dependabot and CodeQL need no secrets.

### Supply chain
- Exact-version pins (`uv.lock`, `pnpm-lock.yaml`, `--frozen-lockfile`, `--locked`).
- GitHub Actions pinned to commit SHAs; Dependabot updates them weekly; `zizmor` lints workflows
  (template injection, unpinned actions, excessive permissions, `pull_request_target` misuse).
- `permissions: contents: read` by default; `persist-credentials: false` on checkout.
- `dependency-review-action` on PRs blocks new deps with known high vulnerabilities or
  disallowed licences (GPL for shipped code is disallowed except tooling).
- Model files (`kokoro`, `whisper`, Ollama digest) verified by SHA-256 in `just models` and
  `chronos doctor`.
- `pnpm` with `minimumReleaseAge` (if supported by the pinned pnpm) or `npm-package-delay` is
  **not** assumed; instead lockfile changes are reviewed as part of the PR ("Dependencies" section).

### Laptop hardening (documented in `10-cicd-and-ops.md`, verified by `chronos doctor`)
- FileVault on; macOS firewall on with an allow rule for the API binary only; API runs as the
  normal user (no sudo) via a user LaunchAgent.
- `scripts/deploy.sh` only fast-forwards `origin/main`, verifies the commit is on `main` of the
  expected remote URL, and never runs scripts from untrusted branches.
- Logs contain no telemetry payloads beyond the derived fields; 14-day rotation.

### Privacy
- Stored fields are limited to `docs/04-data-model.md`; no location, no Wi-Fi identifiers, no
  notification content, no health data. Retention per D24. `chronos privacy export` dumps and
  `chronos privacy purge` deletes all telemetry.

## 3. Security checks in the pipeline

| Where | Check |
|---|---|
| pre-commit | ruff (incl. `S` rules), bandit on `services/api/src`, biome, actionlint, gitleaks |
| `just security` | semgrep (`p/security-audit`, `p/python`, `p/typescript`, `p/secrets`), bandit, pip-audit, pnpm audit, gitleaks (full history) |
| CI required | `api` (bandit + semgrep), `secrets` (gitleaks), `mobile`, `renderer` |
| CI non-required | `audit` weekly (pip-audit, pnpm audit), `zizmor` (workflow lint). CodeQL and dependency review are unavailable without Advanced Security on this private repo |
| Reviewer agent | Manual checklist in `.pi/agents/reviewer.md` (SSRF guard usage, subprocess wrapper, no secret logging, validator presence) |
| Runtime | `chronos doctor` (perms, certs, pins, firewall), `/readyz` |

## 4. Incident response (single user, keep it short)

1. Lost phone → `chronos device revoke` (tokens invalid immediately), re-pair later.
2. Suspected laptop compromise → rotate `CHRONOS_SECRET_KEY` (invalidates signed URLs), revoke
   device, regenerate mkcert CA, rotate Expo/APNs credentials, review `data/logs`.
3. Leaked secret in git → rotate first, then rewrite history (`git filter-repo`), force-push
   requires temporarily disabling the ruleset — document in `docs/decisions/`.
