"""Chronos-AI command-line interface (Typer).

Commands
--------
``chronos serve``                   -- Start the API server with uvicorn.
``chronos db migrate``              -- Apply pending Alembic migrations.
``chronos config validate``         -- Validate YAML config files (stub, T1.4).
``chronos doctor``                  -- Real system health check (T0.6).
``chronos ops in-deploy-window``    -- Exit 0 inside deploy exclusion window.
``chronos pair-code``               -- Generate a pairing code and print/QR it.
``chronos device revoke``           -- Revoke the active device (or all).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC
from pathlib import Path

import typer

# Path to the canonical wake-rules YAML file (tests may monkeypatch this).
# parents[2] of src/chronos/cli.py is services/api/
_WAKE_YAML_PATH: Path = Path(__file__).parents[2] / "config" / "wake_rules.yaml"

app = typer.Typer(
    name="chronos",
    help="Chronos-AI — behavioural wake detection + morning reel API.",
    no_args_is_help=True,
    add_completion=False,
)

# ── db subgroup ───────────────────────────────────────────────────────────────
db_app = typer.Typer(name="db", help="Database management commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")

# ── config subgroup ───────────────────────────────────────────────────────────
config_app = typer.Typer(name="config", help="Config validation commands.", no_args_is_help=True)
app.add_typer(config_app, name="config")

# ── openapi subgroup ─────────────────────────────────────────────────────────
openapi_app = typer.Typer(
    name="openapi",
    help="OpenAPI schema commands.",
    no_args_is_help=True,
)
app.add_typer(openapi_app, name="openapi")


# ── serve ─────────────────────────────────────────────────────────────────────


@app.command()
def serve(
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)."),
    host: str | None = typer.Option(None, "--host", help="Override CHRONOS_API_BIND_HOST."),
    port: int | None = typer.Option(None, "--port", help="Override CHRONOS_API_BIND_PORT."),
    no_scheduler: bool = typer.Option(
        False,
        "--no-scheduler",
        help="Disable the background scheduler (dev / debug).",
    ),
) -> None:
    """Start the Chronos API server with uvicorn."""
    import os

    from chronos.config import get_settings
    from chronos.observability import configure as configure_logging

    if no_scheduler:
        os.environ["CHRONOS_SCHEDULER_ENABLED"] = "false"

    settings = get_settings()
    configure_logging(env=str(settings.env))

    bind_host = host or settings.api_bind_host
    bind_port = port or settings.api_bind_port

    uvicorn_args: list[str] = [
        sys.executable,
        "-m",
        "uvicorn",
        "chronos.main:create_app",
        "--factory",
        "--host",
        bind_host,
        "--port",
        str(bind_port),
    ]

    if reload:
        uvicorn_args.append("--reload")

    if str(settings.env) == "prod":
        # TLS is required in prod; the validator in Settings already checked the files.
        if settings.tls_cert and settings.tls_key:
            uvicorn_args += [
                "--ssl-certfile",
                str(settings.tls_cert),
                "--ssl-keyfile",
                str(settings.tls_key),
            ]
    elif str(settings.env) == "dev" and settings.tls_cert and settings.tls_key:
        # Optional TLS in dev (mkcert certs).
        uvicorn_args += [
            "--ssl-certfile",
            str(settings.tls_cert),
            "--ssl-keyfile",
            str(settings.tls_key),
        ]

    typer.echo(f"Starting Chronos API on {bind_host}:{bind_port} (env={settings.env})")
    result = subprocess.run(uvicorn_args, check=False)
    raise SystemExit(result.returncode)


# ── db migrate ────────────────────────────────────────────────────────────────


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending Alembic migrations (alembic upgrade head)."""
    # parents[2] of src/chronos/cli.py is services/api (where alembic.ini lives).
    alembic_ini = Path(__file__).parents[2] / "alembic.ini"
    if not alembic_ini.exists():
        typer.echo(f"alembic.ini not found at {alembic_ini}", err=True)
        raise typer.Exit(code=1)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "--config", str(alembic_ini), "upgrade", "head"],
        check=False,
    )
    raise SystemExit(result.returncode)


# ── config validate ───────────────────────────────────────────────────────────


@config_app.command("validate")
def config_validate() -> None:
    """Validate config/wake_rules.yaml against the WakeRules schema.

    Exits 0 on success; exits 1 and prints field-level errors on failure.
    """
    import yaml
    from pydantic import ValidationError

    from chronos.wake.rules import load_yaml

    yaml_path = _WAKE_YAML_PATH
    if not yaml_path.exists():
        typer.echo(f"✗ config file not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    try:
        load_yaml(yaml_path)
    except yaml.YAMLError as exc:
        typer.echo(f"✗ YAML parse error in {yaml_path.name}:\n  {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValidationError as exc:
        typer.echo(f"✗ {yaml_path.name} has {exc.error_count()} validation error(s):", err=True)
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            typer.echo(f"  [{loc}] {err['msg']}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # pragma: no cover
        typer.echo(f"✗ Unexpected error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ {yaml_path.name} is valid")


@config_app.command("apply-yaml")
def config_apply_yaml() -> None:
    """Promote config/wake_rules.yaml as a new active rule version.

    Loads and validates the YAML file, then calls activate_new_version with
    ``source='yaml'`` and ``created_by='cli'``.  Prints the resulting version
    number; exits 1 on validation or DB errors.
    """
    import yaml
    from pydantic import ValidationError
    from sqlmodel import Session

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.wake.rules import activate_new_version as _activate
    from chronos.wake.rules import load_yaml

    yaml_path = _WAKE_YAML_PATH
    if not yaml_path.exists():
        typer.echo(f"✗ config file not found: {yaml_path}", err=True)
        raise typer.Exit(code=1)

    try:
        rules = load_yaml(yaml_path)
    except yaml.YAMLError as exc:
        typer.echo(f"✗ YAML parse error in {yaml_path.name}:\n  {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValidationError as exc:
        typer.echo(f"✗ {yaml_path.name} has {exc.error_count()} validation error(s):", err=True)
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            typer.echo(f"  [{loc}] {err['msg']}", err=True)
        raise typer.Exit(code=1) from exc

    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        with Session(engine) as session:
            rv, created = _activate(session, rules, source="yaml", created_by="cli")
            session.commit()
            # Capture before session closes to avoid DetachedInstanceError.
            rv_version = rv.version
            rv_was_created = created
    except Exception as exc:  # pragma: no cover
        typer.echo(f"✗ DB error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if rv_was_created:
        typer.echo(f"✓ Applied wake_rules.yaml as rule version {rv_version}")
    else:
        typer.echo(f"✓ No change — active version {rv_version} already has matching SHA")


# ── pair-code ─────────────────────────────────────────────────────────────────

# ── device subgroup ──────────────────────────────────────────────────────────
device_app = typer.Typer(name="device", help="Device management commands.", no_args_is_help=True)
app.add_typer(device_app, name="device")


@app.command("pair-code")
def pair_code() -> None:
    """Generate a one-time pairing code and display it (plus a QR code).

    Creates a ``pairing_codes`` row (10-minute expiry), prints the
    ``XXXX-XXXX`` code to the terminal and renders a compact QR code whose
    payload is the JSON object::

        {"code": "XXXX-XXXX", "api_base": "http://…/v1"}

    The QR is rendered with ``segno`` (owner answer 1) using the terminal
    pixel-art renderer.
    """
    import json
    from datetime import timedelta

    import segno
    from sqlmodel import Session

    from chronos.auth import generate_pairing_code, hash_token, normalise_pairing_code
    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.models.devices import PairingCode
    from chronos.time import parse_utc_iso, utc_now_iso

    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    raw_code = generate_pairing_code()
    code_hash = hash_token(normalise_pairing_code(raw_code))
    now_iso = utc_now_iso()
    expires_iso = (
        (parse_utc_iso(now_iso) + timedelta(minutes=10))
        .astimezone(UTC)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )

    pc = PairingCode(
        code_hash=code_hash,
        expires_at=expires_iso,
        consumed_at=None,
        attempts=0,
    )

    with Session(engine) as session:
        session.add(pc)
        session.commit()

    display_code = f"{raw_code[:4]}-{raw_code[4:]}"
    api_base = settings.public_base_url
    payload = json.dumps({"code": display_code, "api_base": api_base}, separators=(",", ":"))

    typer.echo(f"\nPairing code : {display_code}")
    typer.echo("Expires in   : 10 minutes")
    typer.echo(f"API base     : {api_base}")
    typer.echo("\nScan the QR code on your iPhone:\n")

    try:
        qr = segno.make(payload, error="M")
        qr.terminal(compact=True)
    except Exception as exc:  # pragma: no cover — segno output is env-dependent
        typer.echo(f"(QR generation skipped: {exc})", err=True)

    typer.echo(f"\nOr enter manually: {display_code}")


# ── device revoke ─────────────────────────────────────────────────────────────


@device_app.command("revoke")
def device_revoke(
    all_devices: bool = typer.Option(False, "--all", help="Revoke all devices (not just active)."),
) -> None:
    """Revoke the active device token (or all devices with ``--all``).

    Sets ``status = revoked`` and ``revoked_at = now`` on every matched device
    row.  Any subsequent request with the old token will receive 403 ``revoked``.
    """
    from sqlmodel import Session, select

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.enums import DeviceStatus
    from chronos.models.devices import Device
    from chronos.time import utc_now_iso

    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    with Session(engine) as session:
        if all_devices:
            devices: list[Device] = list(session.exec(select(Device)).all())
        else:
            devices = list(
                session.exec(select(Device).where(Device.status == DeviceStatus.active)).all()
            )

        if not devices:
            typer.echo("No devices to revoke.")
            return

        now = utc_now_iso()
        for device in devices:
            device.status = DeviceStatus.revoked
            device.revoked_at = now
            session.add(device)
        session.commit()

    count = len(devices)
    typer.echo(f"Revoked {count} device(s).")


# ── ops subgroup ─────────────────────────────────────────────────────────────

ops_app = typer.Typer(name="ops", help="Operational utilities (T0.6).", no_args_is_help=True)
app.add_typer(ops_app, name="ops")


@ops_app.command("in-deploy-window")
def ops_in_deploy_window(
    tz: str = typer.Option(
        "America/Los_Angeles",
        "--tz",
        help="Local IANA timezone (default: America/Los_Angeles).",
    ),
) -> None:
    """Exit 0 if the current time is inside the deploy exclusion window.

    The deploy exclusion window is [window_start - 45 min, window_end).
    scripts/deploy.sh calls this; exit 0 means 'skip the deploy'.
    """
    import datetime
    from zoneinfo import ZoneInfo

    import yaml
    from pydantic import ValidationError

    from chronos.ops.deploy_window import in_deploy_window
    from chronos.wake.rules import load_yaml

    yaml_path = _WAKE_YAML_PATH
    if not yaml_path.exists():
        typer.echo(f"wake_rules.yaml not found at {yaml_path}; assuming outside window", err=True)
        raise typer.Exit(code=1)

    try:
        rules = load_yaml(yaml_path)
    except (yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"Could not load wake_rules.yaml: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        local_tz = ZoneInfo(tz)
    except Exception as exc:
        typer.echo(f"Invalid timezone {tz!r}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    now = datetime.datetime.now(datetime.UTC)
    inside = in_deploy_window(now, rules, local_tz)

    local_now = now.astimezone(local_tz).strftime("%H:%M")
    if inside:
        typer.echo(f"deploy-window: INSIDE exclusion at {local_now} {tz}")
        raise typer.Exit(code=0)  # exit 0 = skip deploy
    else:
        typer.echo(f"deploy-window: outside exclusion at {local_now} {tz}")
        raise typer.Exit(code=1)  # exit 1 = proceed with deploy


# ── doctor ────────────────────────────────────────────────────────────────────


@app.command()
def doctor(
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Repository root (default: auto-detected from cli.py location).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSONL instead of a table."),
) -> None:
    """Check this Mac: certs, models, Ollama, LaunchAgents, pmset, disk, DB.

    Exits 0 if all checks pass or only advisory warnings were found.
    Exits 1 if any blocking failure is detected.
    """
    import json

    from chronos.ops.doctor import FAIL, OK, WARN, DoctorProbes, run_doctor

    # Repo root: services/api is parents[2] from src/chronos/cli.py;
    # the actual repo root is one level above services/api.
    _default_root = Path(__file__).parents[3]
    root = repo_root or _default_root

    # Try to load settings for public_base_url; best-effort.
    try:
        from chronos.config import get_settings

        settings = get_settings()
    except Exception:
        settings = None

    # Read ollama pins from justfile if available.
    ollama_model = "qwen2.5:7b-instruct"
    ollama_digest = ""
    justfile = root / "justfile"
    if justfile.exists():
        for line in justfile.read_text().splitlines():
            if line.strip().startswith("ollama_model"):
                parts = line.split(":=", 1)
                if len(parts) == 2:
                    ollama_model = parts[1].strip().strip('"')
            if line.strip().startswith("ollama_digest"):
                parts = line.split(":=", 1)
                if len(parts) == 2:
                    ollama_digest = parts[1].strip().strip('"')

    rows, has_blocking = run_doctor(
        root,
        settings=settings,
        probes=DoctorProbes(),
        ollama_model=ollama_model,
        ollama_digest=ollama_digest,
    )

    if json_output:
        for row in rows:
            typer.echo(json.dumps({"name": row.name, "status": row.status, "message": row.message}))
    else:
        # Human-readable table
        col_w = max((len(r.name) for r in rows), default=10) + 2
        symbols = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]", "skip": "[skip]"}
        for row in rows:
            sym = symbols.get(row.status, "[????]")
            typer.echo(f"  {sym}  {row.name:<{col_w}} {row.message}")
        typer.echo("")
        if has_blocking:
            typer.echo("chronos doctor: BLOCKING failures found (exit 1)", err=True)
        else:
            typer.echo("chronos doctor: all checks passed (warnings are advisory)")

    raise typer.Exit(code=1 if has_blocking else 0)


# ── openapi export ────────────────────────────────────────────────────────────

# Default output path: services/api/openapi.json relative to this source file.
# cli.py lives at src/chronos/cli.py → parents[0]=chronos, [1]=src, [2]=services/api.
_DEFAULT_OPENAPI_OUT: Path = Path(__file__).parents[2] / "openapi.json"


@openapi_app.command("export")
def openapi_export(
    out: Path = typer.Option(
        _DEFAULT_OPENAPI_OUT,
        "--out",
        help="Output path for the JSON file (default: services/api/openapi.json).",
    ),
) -> None:
    """Export the OpenAPI 3.1 schema to a deterministic JSON file.

    Serialises the FastAPI schema with sorted keys, indent=2 and a trailing
    newline.  The ``info.version`` field comes from the repo-root VERSION file
    (via ``chronos.__version__``).  No network calls are made; the export is
    pure and deterministic.

    Example::

        chronos openapi export
        chronos openapi export --out /tmp/openapi.json
    """
    import json

    from pydantic import SecretStr

    from chronos.config import Settings
    from chronos.main import create_app

    # Minimal settings: no real DB path needed; skip wake-rules init and
    # scheduler so the export function has no side effects.
    # A dummy secret_key is sufficient; the key is never used during schema
    # generation (no auth middleware runs until the lifespan context starts).
    settings = Settings(
        secret_key=SecretStr("dummy-key-for-schema-export"),
        database_url="sqlite:///",
        skip_rules_init=True,
        scheduler_enabled=False,
    )
    fastapi_app = create_app(settings)
    schema: dict[str, object] = fastapi_app.openapi()  # type: ignore[assignment]

    out.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(schema, sort_keys=True, indent=2) + "\n"
    out.write_text(content, encoding="utf-8")
    typer.echo(f"✓ OpenAPI schema written to {out} ({len(content)} bytes)")
