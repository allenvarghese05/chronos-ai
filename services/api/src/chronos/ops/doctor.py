"""Chronos system-health checks for ``chronos doctor`` (T0.6).

All probes are injectable so the test suite never spawns real subprocesses or
reads real files.  The public surface is ``run_doctor`` which accepts a
:class:`DoctorProbes` dataclass and returns ``(rows, has_blocking_failure)``.

Check taxonomy
--------------
- **blocking** (exit 1): missing certs, bad cert mode, corrupt DB, model files
  absent, Ollama unreachable, API LaunchAgent not loaded, disk < 1 GB.
- **advisory** (warn, exit 0): pmset entries missing, firewall unclear,
  FileVault off, disk < 2 GB, .env mode not 600, scheduler disabled,
  ollama digest mismatch, cert expiry < 30 d, public_base_url unresolvable.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

# ---- status constants -------------------------------------------------------

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


@dataclass
class DoctorRow:
    """One row in the doctor output table."""

    name: str
    status: str  # ok | warn | fail | skip
    message: str


# ---- injectable probe types -------------------------------------------------


# Type aliases for the probe callables.
_RunCmd = Callable[[list[str]], tuple[str, int]]
_StatPath = Callable[[Path], Any]
_DiskUsage = Callable[[str], Any]
_GetAddrInfo = Callable[[str, Any], list[Any]]
_HttpGet = Callable[[str], Any]
_CertExpiry = Callable[[Path], datetime.datetime | None]
_ReadModelsLock = Callable[[Path], dict[str, Any] | None]


@dataclass
class DoctorProbes:
    """Callables injected into run_doctor; defaults call real system APIs."""

    run_cmd: _RunCmd = field(default_factory=lambda: _default_run_cmd)
    stat_path: _StatPath = field(default_factory=lambda: _default_stat_path)
    disk_usage: _DiskUsage = field(default_factory=lambda: shutil.disk_usage)  # type: ignore[arg-type]  # shutil.disk_usage accepts str|bytes|PathLike; _DiskUsage(str) is a runtime subset
    getaddrinfo: _GetAddrInfo = field(default_factory=lambda: socket.getaddrinfo)  # type: ignore[arg-type]  # socket.getaddrinfo has overloaded C-level signature; compatible with _GetAddrInfo at runtime
    http_get: _HttpGet = field(default_factory=lambda: _default_http_get)
    cert_expiry: _CertExpiry = field(default_factory=lambda: _default_cert_expiry)
    read_models_lock: _ReadModelsLock = field(default_factory=lambda: _default_read_models_lock)


def _default_stat_path(p: Path) -> Any:
    return p.stat()


def _default_run_cmd(args: list[str]) -> tuple[str, int]:
    """Run a subprocess and return (stdout+stderr, returncode)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.stdout + result.stderr).strip(), result.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return str(exc), 1


def _default_http_get(url: str) -> Any:
    """Perform a real HTTP GET via httpx (import lazily)."""
    try:
        import httpx

        return httpx.get(url, timeout=5)
    except Exception:
        return None


def _default_cert_expiry(cert_path: Path) -> datetime.datetime | None:
    """Parse the notAfter date from a PEM cert file."""
    try:
        # Use openssl to parse expiry date (most reliable across macOS versions).
        out, rc = _default_run_cmd(["openssl", "x509", "-noout", "-enddate", "-in", str(cert_path)])
        if rc != 0:
            return None
        # "notAfter=Oct 21 12:00:00 2027 GMT"
        for line in out.splitlines():
            if "notAfter=" in line or "notafter=" in line.lower():
                date_str = line.split("=", 1)[1].strip()
                return datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=ZoneInfo("UTC")
                )
    except Exception:  # pragma: no cover
        return None  # caller treats None as WARN; no-op log intentional here


def _default_read_models_lock(lock_path: Path) -> dict[str, Any] | None:
    """Read and parse models.lock JSON."""
    if not lock_path.exists():
        return None
    try:
        return json.loads(lock_path.read_text())
    except Exception:  # pragma: no cover
        return None


# ---- check implementations --------------------------------------------------


def check_certs(
    repo_root: Path,
    probes: DoctorProbes,
    now: datetime.datetime,
) -> list[DoctorRow]:
    rows: list[DoctorRow] = []
    cert_dir = repo_root / "data" / "certs"
    cert_file = cert_dir / "chronos-lan.pem"
    key_file = cert_dir / "chronos-lan-key.pem"
    root_ca = cert_dir / "rootCA.pem"

    for label, path in [("cert", cert_file), ("key", key_file), ("rootCA", root_ca)]:
        if not path.exists():
            rows.append(DoctorRow(f"cert:{label}", FAIL, f"Missing: {path}"))
            continue
        try:
            st = probes.stat_path(path)
            mode = st.st_mode & 0o777
            if mode & 0o077:
                rows.append(
                    DoctorRow(
                        f"cert:{label}:mode",
                        FAIL,
                        f"{path.name} mode {oct(mode)} (want 600)",
                    )
                )
            else:
                rows.append(DoctorRow(f"cert:{label}:mode", OK, f"{path.name} mode {oct(mode)}"))
        except OSError as exc:
            rows.append(DoctorRow(f"cert:{label}:mode", FAIL, str(exc)))

    # Expiry check on the leaf cert
    if cert_file.exists():
        expiry = probes.cert_expiry(cert_file)
        if expiry is None:
            rows.append(DoctorRow("cert:expiry", WARN, "Could not determine cert expiry"))
        else:
            days_left = (expiry - now).days
            if days_left < 7:
                rows.append(
                    DoctorRow("cert:expiry", FAIL, f"Cert expires in {days_left} days (< 7)")
                )
            elif days_left < 30:
                rows.append(
                    DoctorRow("cert:expiry", WARN, f"Cert expires in {days_left} days (< 30)")
                )
            else:
                rows.append(DoctorRow("cert:expiry", OK, f"Cert valid for {days_left} more days"))

    return rows


def check_env_mode(repo_root: Path, probes: DoctorProbes) -> DoctorRow:
    env_path = repo_root / "services" / "api" / ".env"
    if not env_path.exists():
        return DoctorRow("env:mode", WARN, f".env not found at {env_path}")
    try:
        st = probes.stat_path(env_path)
        mode = st.st_mode & 0o777
        if mode & 0o077:
            return DoctorRow("env:mode", WARN, f".env mode {oct(mode)} (want 600)")
        return DoctorRow("env:mode", OK, f".env mode {oct(mode)}")
    except OSError as exc:
        return DoctorRow("env:mode", WARN, str(exc))


def check_models(repo_root: Path, probes: DoctorProbes) -> list[DoctorRow]:
    rows: list[DoctorRow] = []
    lock_path = repo_root / "scripts" / "models.lock"
    lock = probes.read_models_lock(lock_path)

    if lock is None:
        rows.append(DoctorRow("models:lock", WARN, f"models.lock not found at {lock_path}"))
        # Still check file existence
        for name, rel in [
            ("kokoro_onnx", "data/models/kokoro/kokoro-v1.0.onnx"),
            ("kokoro_voices", "data/models/kokoro/voices-v1.0.bin"),
            ("whisper_small_en", "data/models/whisper/ggml-small.en.bin"),
        ]:
            p = repo_root / rel
            status = OK if p.exists() else FAIL
            msg = "present" if p.exists() else f"missing: {p}"
            rows.append(DoctorRow(f"models:{name}", status, msg))
        return rows

    for name, entry_raw in lock.items():
        entry: dict[str, str] = (
            cast(dict[str, str], entry_raw) if isinstance(entry_raw, dict) else {}
        )
        rel_path = entry.get("path", "")
        expected_sha = entry.get("sha256", "PLACEHOLDER")
        p = repo_root / rel_path
        if not p.exists():
            rows.append(DoctorRow(f"models:{name}", FAIL, f"missing: {p}"))
            continue
        if expected_sha == "PLACEHOLDER":
            rows.append(DoctorRow(f"models:{name}", WARN, f"{p.name} present (sha not pinned)"))
            continue
        # Verify SHA256 (hashlib imported at module level)
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if sha != expected_sha:
            rows.append(
                DoctorRow(
                    f"models:{name}",
                    FAIL,
                    f"{p.name} SHA mismatch (got {sha[:12]}..., want {expected_sha[:12]}...)",
                )
            )
        else:
            rows.append(DoctorRow(f"models:{name}", OK, f"{p.name} SHA OK"))

    return rows


def check_ollama(probes: DoctorProbes, ollama_model: str, ollama_digest: str) -> list[DoctorRow]:
    rows: list[DoctorRow] = []
    resp = probes.http_get("http://127.0.0.1:11434/api/tags")
    if resp is None or (hasattr(resp, "status_code") and resp.status_code != 200):
        rows.append(DoctorRow("ollama:reachable", FAIL, "Ollama not reachable at :11434"))
        return rows

    rows.append(DoctorRow("ollama:reachable", OK, "Ollama API responding"))
    try:
        raw: Any = resp.json() if hasattr(resp, "json") else {}
        body: dict[str, Any] = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        models_raw: Any = body.get("models", [])
        _raw_list: list[Any] = cast(list[Any], models_raw) if isinstance(models_raw, list) else []
        models_list: list[dict[str, Any]] = [
            cast(dict[str, Any], m) for m in _raw_list if isinstance(m, dict)
        ]
        found: dict[str, Any] | None = next(
            (
                m
                for m in models_list
                if str(m.get("name", "")).startswith(ollama_model.split(":")[0])
            ),
            None,
        )
        if found is None:
            rows.append(
                DoctorRow("ollama:model", FAIL, f"Model {ollama_model!r} not found in ollama list")
            )
            return rows
        model_name = str(found.get("name", ""))
        rows.append(DoctorRow("ollama:model", OK, f"Model {model_name!r} present"))

        if ollama_digest:
            actual_digest = str(found.get("digest", ""))
            if actual_digest == ollama_digest:
                rows.append(
                    DoctorRow(
                        "ollama:digest",
                        OK,
                        f"Digest matches {ollama_digest[:20]}...",
                    )
                )
            else:
                rows.append(
                    DoctorRow(
                        "ollama:digest",
                        WARN,
                        f"Mismatch: got {actual_digest[:20]}..., want {ollama_digest[:20]}...",
                    )
                )
        else:
            rows.append(DoctorRow("ollama:digest", WARN, "ollama_digest not pinned in justfile"))
    except Exception as exc:
        rows.append(DoctorRow("ollama:model", WARN, f"Could not parse ollama tags: {exc}"))

    return rows


def check_launchagents(probes: DoctorProbes) -> list[DoctorRow]:
    rows: list[DoctorRow] = []
    out, rc = probes.run_cmd(["launchctl", "list"])
    if rc != 0:
        rows.append(DoctorRow("launchd:api", FAIL, "launchctl list failed"))
        rows.append(DoctorRow("launchd:deploy", FAIL, "launchctl list failed"))
        return rows

    for label in ["com.chronos.api", "com.chronos.deploy"]:
        key = "launchd:" + label.split(".")[2]
        if label in out:
            rows.append(DoctorRow(key, OK, f"{label} loaded"))
        else:
            status = FAIL if label == "com.chronos.api" else WARN
            rows.append(DoctorRow(key, status, f"{label} NOT in launchctl list"))

    return rows


def check_pmset(probes: DoctorProbes) -> DoctorRow:
    out, rc = probes.run_cmd(["pmset", "-g", "sched"])
    if rc != 0:
        return DoctorRow("pmset", WARN, "pmset -g sched failed")
    # Look for wakeorpoweron entries at 05:25 (weekday) and 07:25 (weekend)
    has_weekday = "05:25" in out
    has_weekend = "07:25" in out
    if has_weekday and has_weekend:
        return DoctorRow("pmset", OK, "weekday 05:25 and weekend 07:25 wake entries present")
    missing: list[str] = []
    if not has_weekday:
        missing.append("weekday 05:25")
    if not has_weekend:
        missing.append("weekend 07:25")
    return DoctorRow("pmset", WARN, f"pmset missing entries: {', '.join(missing)}")


def check_firewall(probes: DoctorProbes) -> DoctorRow:
    fw_cmd = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    out, rc = probes.run_cmd([fw_cmd, "--getglobalstate"])
    if rc != 0:
        return DoctorRow("firewall", WARN, "Could not query firewall state")
    if "enabled" in out.lower():
        return DoctorRow(
            "firewall",
            WARN,
            "Firewall is ON; verify port 8443 rule (`sudo socketfilterfw --add $(which uv)`)",
        )
    return DoctorRow("firewall", WARN, "Firewall is OFF (acceptable on private LAN)")


def check_filevault(probes: DoctorProbes) -> DoctorRow:
    out, rc = probes.run_cmd(["fdesetup", "status"])
    if rc != 0:
        return DoctorRow("filevault", WARN, "fdesetup status failed")
    if "on" in out.lower():
        return DoctorRow("filevault", OK, "FileVault is On")
    return DoctorRow("filevault", WARN, "FileVault is Off (recommend enabling)")


def check_disk(repo_root: Path, probes: DoctorProbes) -> DoctorRow:
    usage = probes.disk_usage(str(repo_root))
    free_gb = usage.free / (1024**3)
    if free_gb < 1:
        return DoctorRow("disk", FAIL, f"Free space {free_gb:.1f} GB (< 1 GB; blocking)")
    if free_gb < 2:
        return DoctorRow("disk", WARN, f"Free space {free_gb:.1f} GB (< 2 GB)")
    return DoctorRow("disk", OK, f"Free space {free_gb:.1f} GB")


def check_db(repo_root: Path, probes: DoctorProbes) -> DoctorRow:
    db_path = repo_root / "data" / "chronos.db"
    if not db_path.exists():
        return DoctorRow("db", WARN, f"Database not found at {db_path} (run: just migrate)")
    out, rc = probes.run_cmd(["sqlite3", str(db_path), "PRAGMA integrity_check;"])
    if rc != 0 or "ok" not in out.lower():
        return DoctorRow("db", FAIL, f"DB integrity_check failed: {out[:200]}")
    return DoctorRow("db", OK, "DB integrity_check: ok")


def check_public_base_url(public_base_url: str, probes: DoctorProbes) -> DoctorRow:
    if not public_base_url:
        return DoctorRow("public_base_url", WARN, "CHRONOS_PUBLIC_BASE_URL not set")
    try:
        from urllib.parse import urlparse

        parsed = urlparse(public_base_url)
        host = parsed.hostname or ""
        probes.getaddrinfo(host, None)
        return DoctorRow("public_base_url", OK, f"{host} resolves")
    except OSError:
        return DoctorRow("public_base_url", WARN, f"Cannot resolve host in {public_base_url!r}")
    except Exception as exc:
        return DoctorRow("public_base_url", WARN, str(exc))


# ---- main entry point -------------------------------------------------------


def run_doctor(
    repo_root: Path,
    *,
    settings: Any = None,
    probes: DoctorProbes | None = None,
    ollama_model: str = "qwen2.5:7b-instruct",
    ollama_digest: str = "",
    now: datetime.datetime | None = None,
) -> tuple[list[DoctorRow], bool]:
    """Run all doctor checks and return ``(rows, has_blocking_failure)``.

    Args:
        repo_root: Absolute path to the repository root.
        settings: Optional :class:`~chronos.config.Settings` instance.
            Used for ``public_base_url``; loaded lazily if not supplied.
        probes: Injectable probe functions (real system calls by default).
        ollama_model: Expected Ollama model name (from justfile).
        ollama_digest: Expected Ollama digest (from justfile; may be empty).
        now: Fixed UTC clock for testing; defaults to ``datetime.datetime.now(UTC)``.

    Returns:
        A tuple ``(rows, has_blocking_failure)`` where *has_blocking_failure*
        is ``True`` iff any row has ``status == 'fail'``.
    """
    if probes is None:
        probes = DoctorProbes()
    from datetime import UTC

    clock = now or datetime.datetime.now(UTC)

    rows: list[DoctorRow] = []

    # 1. Certs
    rows.extend(check_certs(repo_root, probes, clock))

    # 2. .env mode
    rows.append(check_env_mode(repo_root, probes))

    # 3. Model files vs lock
    rows.extend(check_models(repo_root, probes))

    # 4. Ollama
    rows.extend(check_ollama(probes, ollama_model, ollama_digest))

    # 5. LaunchAgents
    rows.extend(check_launchagents(probes))

    # 6. pmset schedule
    rows.append(check_pmset(probes))

    # 7. Firewall
    rows.append(check_firewall(probes))

    # 8. FileVault
    rows.append(check_filevault(probes))

    # 9. Disk space
    rows.append(check_disk(repo_root, probes))

    # 10. DB integrity
    rows.append(check_db(repo_root, probes))

    # 11. public_base_url
    pub_url = ""
    if settings is not None:
        pub_url = getattr(settings, "public_base_url", "")
    rows.append(check_public_base_url(pub_url, probes))

    has_blocking = any(r.status == FAIL for r in rows)
    return rows, has_blocking
