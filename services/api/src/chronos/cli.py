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
``chronos wake replay``             -- Re-score stored mornings under two rule versions (T1.8).
``chronos wake annotate``           -- Record actual wake time for a local date (T1.8).
``chronos report build``            -- Build the nightly Markdown report for a date (T1.8).
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
_SOURCES_YAML_PATH: Path = Path(__file__).parents[2] / "config" / "sources.yaml"

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
    no_worker: bool = typer.Option(
        False,
        "--no-worker",
        help="Disable the in-process serial job worker (dev / debug).",
    ),
) -> None:
    """Start the Chronos API server with uvicorn."""
    import os

    from chronos.config import get_settings
    from chronos.observability import configure as configure_logging

    if no_scheduler:
        os.environ["CHRONOS_SCHEDULER_ENABLED"] = "false"
    if no_worker:
        os.environ["CHRONOS_WORKER_ENABLED"] = "false"

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
    """Validate config/wake_rules.yaml and config/sources.yaml.

    Exits 0 on success; exits 1 and prints field-level errors on failure.
    """
    import yaml
    from pydantic import ValidationError

    from chronos.content.sources import load_sources_yaml
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

    sources_path = _SOURCES_YAML_PATH
    if not sources_path.exists():
        typer.echo(f"✗ config file not found: {sources_path}", err=True)
        raise typer.Exit(code=1)

    try:
        load_sources_yaml(sources_path)
    except yaml.YAMLError as exc:
        typer.echo(f"✗ YAML parse error in {sources_path.name}:\n  {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValidationError as exc:
        typer.echo(f"✗ {sources_path.name} has {exc.error_count()} validation error(s):", err=True)
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            typer.echo(f"  [{loc}] {err['msg']}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # pragma: no cover
        typer.echo(f"✗ Unexpected error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ {sources_path.name} is valid")


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


# ── run (pipeline) ────────────────────────────────────────────────────────────


@app.command("run")
def run_pipeline(
    date: str = typer.Option("today", "--date", help="Local day YYYY-MM-DD or 'today'."),
    stages: str = typer.Option(
        "ingest,script", "--stages", help="Comma list; default the content stages."
    ),
    force: bool = typer.Option(False, "--force", help="Re-run stages that already ran."),
    device_id: int | None = typer.Option(
        None, "--device", help="Device id (default: the paired one)."
    ),
    timeout: float = typer.Option(1800.0, "--timeout", help="Seconds to wait for the chain."),
) -> None:
    """Run pipeline stages for one local day and wait for them (T3.5).

    Idempotent: stages that already completed for that day are not re-run unless --force.
    """
    import datetime as _dt

    from sqlmodel import Session, select

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.enums import JobKind
    from chronos.models.devices import Device
    from chronos.pipeline import STAGE_ORDER, ensure_day, implemented, run_chain
    from chronos.wake.rules import ensure_initial_version

    try:
        wanted = [JobKind(s.strip()) for s in stages.split(",") if s.strip()]
    except ValueError as exc:
        typer.echo(f"✗ unknown stage in {stages!r}; stages: {[s.value for s in STAGE_ORDER]}",
                   err=True)  # fmt: skip
        raise typer.Exit(code=2) from exc
    runnable = implemented(wanted)
    skipped = [s.value for s in wanted if s not in runnable]
    if skipped:
        typer.echo(f"! not implemented yet, skipped: {', '.join(skipped)}", err=True)
    if not runnable:
        raise typer.Exit(code=2)

    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    now = _dt.datetime.now(_dt.UTC)
    with Session(engine) as session:
        ensure_initial_version(session, settings.wake_rules_path)
        session.commit()
        query = select(Device).where(Device.status == "active")
        if device_id is not None:
            query = query.where(Device.id == device_id)
        devices = session.exec(query).all()
        if len(devices) != 1:
            typer.echo(f"✗ need exactly one active device (found {len(devices)}); use --device",
                       err=True)  # fmt: skip
            raise typer.Exit(code=1)
        device = devices[0]
        assert device.id is not None
        chosen_id: int = device.id
        local_date = ensure_day(
            session, device, now, None if date == "today" else _dt.date.fromisoformat(date)
        )
    typer.echo(f"→ {local_date} device {chosen_id}: {', '.join(s.value for s in runnable)}")
    results = run_chain(
        engine, settings.data_root.resolve(), settings, local_date=local_date, device_id=chosen_id,
        stages=runnable, force=force, timeout_sec=timeout,
    )  # fmt: skip
    failed = False
    for r in results:
        mark = {"done": "✓", "failed": "✗", "cancelled": "✗"}.get(r.status, "…")
        took = f" {r.duration_ms / 1000:.1f}s" if r.duration_ms is not None else ""
        typer.echo(f"  {mark} {r.stage:9} {r.status}{took}" + (f"  {r.error}" if r.error else ""))
        failed |= r.status != "done"
    engine.dispose()
    if failed:
        raise typer.Exit(code=1)


# ── simulate-morning (T5.5) ───────────────────────────────────────────────────


@app.command("simulate-morning")
def simulate_morning_cmd(
    date: str = typer.Option("today", "--date", help="Local day YYYY-MM-DD or 'today'."),
    wake: str = typer.Option(
        "06:40", "--wake", help="When the simulated owner gets up (local HH:MM)."
    ),
    tz: str = typer.Option("Asia/Kolkata", "--tz", help="IANA timezone of the simulated phone."),
    keep: Path | None = typer.Option(
        None, "--keep-dir", help="Use/keep this dir instead of a temp one."
    ),
) -> None:
    """Dry-run a whole morning (scheduler, wake engine, real pipeline, delivery) on a fake clock.

    Always uses an isolated database and data directory; the live ones are never touched.
    Exits 0 when every check passes.
    """
    import asyncio
    import datetime as _dt
    import tempfile
    from zoneinfo import ZoneInfo

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from chronos.config import get_settings
    from chronos.simulate import render_report, simulate_morning

    root = keep or Path(tempfile.mkdtemp(prefix="chronos-sim-"))
    root.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{root / 'sim.db'}"
    cfg = AlembicConfig(str(Path(__file__).parents[2] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")
    settings = get_settings().model_copy(
        update={"database_url": url, "data_root": root / "data", "reports_dir": root / "reports",
                "scheduler_enabled": False, "worker_enabled": False}
    )  # fmt: skip
    day = _dt.datetime.now(ZoneInfo(tz)).date() if date == "today" else _dt.date.fromisoformat(date)
    hour, minute = (int(x) for x in wake.split(":"))
    report = asyncio.run(
        simulate_morning(settings, local_date=day, wake_local=_dt.time(hour, minute), tz=tz)
    )
    typer.echo(render_report(report))
    typer.echo(f"\n(simulation data kept in {root})")
    if not all(report.checks().values()):
        raise typer.Exit(code=1)


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

    # cli.py lives at services/api/src/chronos/cli.py → parents[4] = repo root
    _default_root = Path(__file__).parents[4]
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


schema_app = typer.Typer(name="schema", help="Renderer schema export (T3.4).", no_args_is_help=True)
app.add_typer(schema_app)
_DEFAULT_ZOD_OUT: Path = (
    Path(__file__).parents[4] / "packages" / "renderer" / "src" / "schema.generated.ts"
)
_DEFAULT_REEL_SCHEMA_OUT: Path = Path(__file__).parents[2] / "reel_props.schema.json"


@schema_app.command("export-reel-props")
def schema_export_reel_props(
    zod_out: Path = typer.Option(_DEFAULT_ZOD_OUT, "--zod-out", help="Generated zod module."),
    json_out: Path = typer.Option(
        _DEFAULT_REEL_SCHEMA_OUT, "--json-out", help="ReelProps JSON schema."
    ),
) -> None:
    """Write the ReelProps JSON schema and its generated zod module (deterministic)."""
    from chronos.schema_export import schema_json_text, zod_source

    for path in (zod_out, json_out):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(schema_json_text(), encoding="utf-8")
    zod_out.write_text(zod_source(), encoding="utf-8")
    typer.echo(f"✓ wrote {json_out} and {zod_out}")


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


# ── wake subgroup ─────────────────────────────────────────────────────────────
wake_app = typer.Typer(name="wake", help="Wake-engine commands (T1.8).", no_args_is_help=True)
app.add_typer(wake_app, name="wake")


@wake_app.command("replay")
def wake_replay(
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)."),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)."),
    device_public_id: str | None = typer.Option(None, "--device", help="Device public_id."),
    rules_path: Path | None = typer.Option(None, "--rules", help="Candidate rules YAML path."),
    rule_version: int | None = typer.Option(
        None, "--rule-version", help="Candidate rule version number."
    ),
    early_min: int = typer.Option(30, "--early-min", help="Early-confirm threshold (minutes)."),
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of a table."),
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Fixture JSON path (mutually exclusive with --from/--to DB query)."
    ),
) -> None:
    """Re-score stored mornings under two rule versions and print a comparison table."""
    import datetime as _dt
    import json as _json

    from sqlmodel import Session, select

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.models.devices import Device
    from chronos.wake.replay import (
        build_comparisons,
        candidate_rules_from_path_or_version,
        load_fixture,
        replay_db_range,
        replay_fixture,
        summary_line,
    )
    from chronos.wake.rules import load_yaml

    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    try:
        from_dt = _dt.date.fromisoformat(from_date)
        to_dt = _dt.date.fromisoformat(to_date)
    except ValueError as exc:
        typer.echo(f"Error: invalid date format: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if fixture is not None:
        # Fixture mode: no DB query needed.
        if not fixture.exists():
            typer.echo(f"Error: fixture file not found: {fixture}", err=True)
            raise typer.Exit(code=1)

        baseline_rules = load_yaml(_WAKE_YAML_PATH)
        if rules_path is not None:
            candidate_rules_obj = load_yaml(rules_path)
        else:
            candidate_rules_obj = baseline_rules

        fd = load_fixture(fixture)
        replay_day = replay_fixture(
            fd, baseline_rules=baseline_rules, candidate_rules=candidate_rules_obj
        )
        replay_days = [replay_day]
    else:
        # DB mode.
        with Session(engine) as session:
            # Resolve device.
            if device_public_id:
                dev_row = session.exec(  # type: ignore[call-overload]
                    select(Device).where(Device.public_id == device_public_id)
                ).first()
            else:
                devs = list(session.exec(select(Device).where(Device.status == "active")).all())  # type: ignore[call-overload]
                if len(devs) == 0:
                    typer.echo("Error: no active devices found.", err=True)
                    raise typer.Exit(code=1)
                if len(devs) > 1:
                    typer.echo("Error: multiple active devices; specify --device.", err=True)
                    raise typer.Exit(code=1)
                dev_row = devs[0]

            if dev_row is None:
                typer.echo("Error: device not found.", err=True)
                raise typer.Exit(code=1)
            assert dev_row.id is not None

            candidate_rules_obj, candidate_version = candidate_rules_from_path_or_version(
                session, rules_path, rule_version
            )
            replay_days = replay_db_range(
                session=session,
                device_id=dev_row.id,
                from_date=from_dt,
                to_date=to_dt,
                candidate_rules=candidate_rules_obj,
                candidate_rule_version_number=candidate_version,
            )

    comparisons = build_comparisons(replay_days, candidate_rules_obj, early_min=early_min)

    if json_output:
        rows: list[dict[str, object]] = []
        for c in comparisons:
            b_str = c.baseline_confirm_time.strftime("%H:%M") if c.baseline_confirm_time else None
            ca_str = (
                c.candidate_confirm_time.strftime("%H:%M") if c.candidate_confirm_time else None
            )
            rows.append(
                {
                    "local_date": c.local_date.isoformat(),
                    "kind": c.kind,
                    "actual_wake_local": c.actual_wake_local,
                    "baseline_confirm": b_str,
                    "candidate_confirm": ca_str,
                    "latency_min": c.latency_min,
                    "ticks_ge_confirm": c.ticks_ge_confirm,
                    "is_early": c.is_early,
                }
            )
        out_dict: dict[str, object] = {"days": rows, "summary": summary_line(comparisons)}
        typer.echo(_json.dumps(out_dict, indent=2))
        return

    # Rich table output.
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="Replay Comparison", show_lines=True)
        table.add_column("date", style="cyan")
        table.add_column("kind")
        table.add_column("actual_wake")
        table.add_column("baseline_confirm")
        table.add_column("candidate_confirm")
        table.add_column("latency_min")
        table.add_column("ticks>=confirm")
        table.add_column("early?")

        def _fmt_confirm(t: object) -> str:
            if t is None:
                return "(no confirm)"
            import datetime as _dt2

            if isinstance(t, _dt2.time):
                return t.strftime("%H:%M")
            return str(t)

        for c in comparisons:
            table.add_row(
                c.local_date.isoformat(),
                c.kind or "?",
                c.actual_wake_local or "n/a",
                _fmt_confirm(c.baseline_confirm_time),
                _fmt_confirm(c.candidate_confirm_time),
                str(c.latency_min) if c.latency_min is not None else "n/a",
                str(c.ticks_ge_confirm),
                "yes" if c.is_early else "",
            )

        console = Console()
        console.print(table)
    except ImportError:
        # Fallback: plain text.
        c0, c1, c2, c3, c4, c5, c6, c7 = (
            "date",
            "kind",
            "actual",
            "baseline",
            "candidate",
            "latency",
            "ticks>=c",
            "early",
        )
        header = f"{c0:<12} {c1:<8} {c2:<8} {c3:<10} {c4:<10} {c5:>8} {c6:>8} {c7}"
        typer.echo(header)
        typer.echo("-" * len(header))
        for c in comparisons:
            b_str = c.baseline_confirm_time.strftime("%H:%M") if c.baseline_confirm_time else "-"
            ca_str = c.candidate_confirm_time.strftime("%H:%M") if c.candidate_confirm_time else "-"
            typer.echo(
                f"{c.local_date.isoformat():<12} {c.kind:<8} "
                f"{c.actual_wake_local or 'n/a':<8} {b_str:<10} {ca_str:<10} "
                f"{str(c.latency_min) if c.latency_min is not None else 'n/a':>8} "
                f"{c.ticks_ge_confirm:>8} {'yes' if c.is_early else ''}"
            )

    typer.echo(f"\nSummary: {summary_line(comparisons)}")


@wake_app.command("annotate")
def wake_annotate(
    date_str: str = typer.Argument(..., help="Local date (YYYY-MM-DD)."),
    actual: str = typer.Option(..., "--actual", help="Actual wake time (HH:MM, 24-hour)."),
    device_public_id: str | None = typer.Option(None, "--device", help="Device public_id."),
) -> None:
    """Record the owner's actual wake time for a local date.

    Updates ``days.actual_wake_local`` for the given date.
    Idempotent: overwrites any previous annotation.
    """
    import datetime as _dt
    import re

    from sqlmodel import Session, select

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.models.devices import Device
    from chronos.models.wake import Day
    from chronos.time import utc_now_iso

    # Validate date.
    try:
        _dt.date.fromisoformat(date_str)
    except ValueError as exc:
        typer.echo(f"Error: invalid date {date_str!r}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Validate HH:MM.
    if not re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", actual):
        typer.echo(
            f"Error: --actual must be HH:MM (24-hour), e.g. '07:05'; got {actual!r}", err=True
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    with Session(engine) as session:
        # Resolve device.
        if device_public_id:
            dev_row = session.exec(  # type: ignore[call-overload]
                select(Device).where(Device.public_id == device_public_id)
            ).first()
        else:
            devs = list(session.exec(select(Device).where(Device.status == "active")).all())  # type: ignore[call-overload]
            if len(devs) == 0:
                typer.echo("Error: no active devices found.", err=True)
                raise typer.Exit(code=1)
            if len(devs) > 1:
                typer.echo("Error: multiple active devices; specify --device.", err=True)
                raise typer.Exit(code=1)
            dev_row = devs[0]

        if dev_row is None:
            typer.echo("Error: device not found.", err=True)
            raise typer.Exit(code=1)
        assert dev_row.id is not None

        day = session.exec(  # type: ignore[call-overload]
            select(Day).where(Day.local_date == date_str, Day.device_id == dev_row.id)
        ).first()
        if day is None:
            typer.echo(
                f"Error: no day row for {date_str!r}. Has any telemetry been received?", err=True
            )
            raise typer.Exit(code=1)

        day.actual_wake_local = actual
        day.updated_at = utc_now_iso()
        session.add(day)
        session.commit()

    typer.echo(f"✓ Annotated {date_str}: actual_wake_local = {actual}")


# ── report subgroup ───────────────────────────────────────────────────────────
report_app = typer.Typer(name="report", help="Report commands (T1.8).", no_args_is_help=True)
app.add_typer(report_app, name="report")


@report_app.command("build")
def report_build(
    date_str: str | None = typer.Option(
        None, "--date", help="Local date (YYYY-MM-DD). Default: yesterday."
    ),
    device_public_id: str | None = typer.Option(None, "--device", help="Device public_id."),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print to stdout instead of writing to file."
    ),
) -> None:
    """Build the Markdown report for a local date.

    Writes to ``settings.reports_dir/{date}.md`` unless ``--stdout`` is set.
    """
    import datetime as _dt
    from pathlib import Path as _Path

    from sqlmodel import Session, select

    from chronos.config import get_settings
    from chronos.db import create_db_engine
    from chronos.models.devices import Device
    from chronos.report.builder import build_day_report, write_day_report

    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    # Resolve date.
    if date_str is None:
        local_date = _dt.date.today() - _dt.timedelta(days=1)
    else:
        try:
            local_date = _dt.date.fromisoformat(date_str)
        except ValueError as exc:
            typer.echo(f"Error: invalid date {date_str!r}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    with Session(engine) as session:
        # Resolve device.
        if device_public_id:
            dev_row = session.exec(  # type: ignore[call-overload]
                select(Device).where(Device.public_id == device_public_id)
            ).first()
        else:
            devs = list(session.exec(select(Device).where(Device.status == "active")).all())  # type: ignore[call-overload]
            if len(devs) == 0:
                typer.echo("Error: no active devices found.", err=True)
                raise typer.Exit(code=1)
            if len(devs) > 1:
                typer.echo("Error: multiple active devices; specify --device.", err=True)
                raise typer.Exit(code=1)
            dev_row = devs[0]

        if dev_row is None:
            typer.echo("Error: device not found.", err=True)
            raise typer.Exit(code=1)
        assert dev_row.id is not None

        now = _dt.datetime.now(tz=_dt.UTC)
        try:
            content = build_day_report(session, dev_row.id, local_date, now)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if stdout:
            typer.echo(content)
            return

        report_path = write_day_report(content, local_date, _Path(settings.reports_dir))
        typer.echo(f"✓ Report written to {report_path}")
