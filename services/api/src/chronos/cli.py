"""Chronos-AI command-line interface (Typer).

Commands
--------
``chronos serve``            — Start the API server with uvicorn.
``chronos db migrate``       — Apply pending Alembic migrations.
``chronos config validate``  — Validate YAML config files (stub, T1.4).
``chronos doctor``           — System health check (stub, T0.6).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

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
) -> None:
    """Start the Chronos API server with uvicorn."""
    from chronos.config import get_settings
    from chronos.observability import configure as configure_logging

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
    """Validate YAML config files (wake rules, sources).

    This is a stub; full validation is implemented in T1.4.
    """
    typer.echo("no config files yet (T1.4)")


# ── doctor ────────────────────────────────────────────────────────────────────


@app.command()
def doctor() -> None:
    """Check this Mac: pins, models, certs, DB. (Stub — implemented in T0.6.)"""
    typer.echo("doctor: not yet implemented (T0.6)")
