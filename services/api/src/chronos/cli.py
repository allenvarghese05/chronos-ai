"""Chronos-AI command-line interface (Typer).

Commands
--------
``chronos serve``            — Start the API server with uvicorn.
``chronos db migrate``       — Apply pending Alembic migrations.
``chronos config validate``  — Validate YAML config files (stub, T1.4).
``chronos doctor``           — System health check (stub, T0.6).
``chronos pair-code``        — Generate a pairing code and print/QR it.
``chronos device revoke``    — Revoke the active device (or all).
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
    alembic_ini = Path(__file__).parents[3] / "alembic.ini"
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


# ── doctor ────────────────────────────────────────────────────────────────────


@app.command()
def doctor() -> None:
    """Check this Mac: pins, models, certs, DB. (Stub — implemented in T0.6.)"""
    typer.echo("doctor: not yet implemented (T0.6)")
