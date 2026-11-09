"""T5.1: signed media + Range, reels API, retention (frozen clock), backup."""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.delivery.retention import backup_database, run_retention
from chronos.ids import new_ulid
from chronos.main import create_app
from chronos.media.serving import (
    ByteRange,
    RangeNotSatisfiable,
    parse_range,
    sign,
    signed_url,
    valid_name,
)
from chronos.models.content import Brief, BriefItem, Item, Source
from chronos.models.jobs import Job
from chronos.models.media import Reel, ReelAsset
from chronos.time import format_utc_iso
from tests.conftest import make_settings
from tests.factories import make_day, make_device
from tests.test_config_wake import auth_headers, insert_device_for_test

NOW = datetime.datetime.now(datetime.UTC)
RID = "01M6ZGPR87B1GED8CV5QYJJ7RA"
MP4 = bytes(range(256)) * 40  # 10 240 bytes of distinguishable content


# --- pure signing / ranges ---------------------------------------------------------------------


def test_sign_is_deterministic_and_bound_to_inputs() -> None:
    a = sign("s", "01ABC", "reel.mp4", 100)
    assert a == sign("s", "01ABC", "reel.mp4", 100) and len(a) == 64
    assert a != sign("s", "01ABC", "thumb.jpg", 100) != sign("s", "01ABC", "reel.mp4", 101)
    assert a != sign("other", "01ABC", "reel.mp4", 100)


def test_signed_url_shape() -> None:
    url = signed_url("https://h:8443/v1", "s", "01M6ZGPR87B1GED8CV5QYJJ7RA", "reel.mp4", 1000)
    assert url.startswith(
        "https://h:8443/v1/media/01M6ZGPR87B1GED8CV5QYJJ7RA/reel.mp4?exp=4600&sig="
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("bytes=0-99", ByteRange(0, 99)), ("bytes=100-", ByteRange(100, 999)),
     ("bytes=-50", ByteRange(950, 999)), ("bytes=990-5000", ByteRange(990, 999))],
)  # fmt: skip
def test_parse_range(header: str | None, expected: ByteRange | None) -> None:
    assert parse_range(header, 1000) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5-2", "bytes=0-1,5-6", "items=0-1",
                                    "bytes=-", "bytes=-0"])  # fmt: skip
def test_parse_range_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, 1000)


@pytest.mark.parametrize(
    ("public_id", "file", "ok"),
    [(RID, "reel.mp4", True), (RID, "reel.json", False), (RID, "../../.env", False),
     ("../x", "reel.mp4", False), (RID.lower(), "reel.mp4", False)],
)  # fmt: skip
def test_valid_name_whitelist(public_id: str, file: str, ok: bool) -> None:
    assert valid_name(public_id, file) is ok


# --- fixtures: a device, a day, a brief, a ready reel with files --------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[Settings, TestClient, str, Reel]]:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'r.db'}"
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    settings = make_settings(database_url=url, data_root=str(tmp_path / "data"),
                             public_base_url="https://chronos.test:8443/v1")  # fmt: skip
    token, device = insert_device_for_test(settings)
    with Session(create_db_engine(url), expire_on_commit=False) as session:
        reel = _make_reel(session, settings, device.id or 0, "2026-10-06", saved=0,
                          created=NOW, expires=NOW + datetime.timedelta(hours=20))  # fmt: skip
    with TestClient(create_app(settings)) as client:
        yield settings, client, token, reel


def _make_reel(
    session: Session,
    settings: Settings,
    device_id: int,
    local_date: str,
    *,
    saved: int,
    created: datetime.datetime,
    expires: datetime.datetime,
) -> Reel:
    from chronos.models.devices import Device

    device = session.get(Device, device_id)
    assert device is not None
    make_day(session, device, local_date=local_date)
    stamp = format_utc_iso(created)
    brief = Brief(local_date=local_date, device_id=device_id, status="ready",
                  created_at=stamp, updated_at=stamp)  # fmt: skip
    session.add(brief)
    session.flush()
    reel = Reel(public_id=new_ulid(), brief_id=brief.id or 0, local_date=local_date,
                device_id=device_id, status="ready", saved=saved, duration_sec=12.0,
                expires_at=format_utc_iso(expires), created_at=format_utc_iso(created),
                updated_at=format_utc_iso(created))  # fmt: skip
    session.add(reel)
    session.commit()
    session.refresh(reel)
    folder = settings.data_root / "media" / local_date / reel.public_id
    folder.mkdir(parents=True)
    (folder / "reel.mp4").write_bytes(MP4)
    (folder / "thumb.jpg").write_bytes(b"jpg")
    (folder / "captions.json").write_text('{"scenes": []}')
    props = {"scenes": [{"index": 0, "type": "cover", "title": "Morning Reel", "startSec": 0,
                         "endSec": 3}, {"index": 1, "type": "story", "title": "A story",
                         "source": {"name": "BBC News", "url": "https://bbc.co.uk/x"},
                         "startSec": 3, "endSec": 12}]}  # fmt: skip
    (folder / "reel.json").write_text(json.dumps(props))
    session.add(ReelAsset(reel_id=reel.id or 0, kind="thumbnail",
                          path=f"{local_date}/{reel.public_id}/thumb.jpg", sha256="x"))  # fmt: skip
    session.commit()
    return reel


def _media_url(
    settings: Settings, reel: Reel, file: str = "reel.mp4", exp_delta: int = 3600
) -> str:
    exp = int(NOW.timestamp()) + exp_delta
    sig = sign(settings.secret_key.get_secret_value(), reel.public_id, file, exp)
    return f"/v1/media/{reel.public_id}/{file}?exp={exp}&sig={sig}"


# --- media endpoint ------------------------------------------------------------------------------


def test_media_full_and_range(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, _, reel = env
    full = client.get(_media_url(settings, reel))
    assert full.status_code == 200 and full.content == MP4
    assert full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"
    assert full.headers["cache-control"] == "private, max-age=0"
    part = client.get(_media_url(settings, reel), headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == MP4[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(MP4)}"
    tail = client.get(_media_url(settings, reel), headers={"Range": "bytes=-10"})
    assert tail.content == MP4[-10:]
    bad = client.get(_media_url(settings, reel), headers={"Range": "bytes=99999-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{len(MP4)}"


def test_expired_signature_is_410(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, _, reel = env
    response = client.get(_media_url(settings, reel, exp_delta=-5))
    assert response.status_code == 410 and response.json()["code"] == "media_expired"


def test_bad_signature_and_traversal_are_404(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, _, reel = env
    url = _media_url(settings, reel)
    assert client.get(url.replace("sig=", "sig=0")[: len(url)]).status_code == 404
    exp = int(NOW.timestamp()) + 60
    for file in ("..%2F..%2Fr.db", "reel.json", "..", "%2e%2e%2fsecret"):
        sig = sign(settings.secret_key.get_secret_value(), reel.public_id, file, exp)
        r = client.get(f"/v1/media/{reel.public_id}/{file}?exp={exp}&sig={sig}")
        assert r.status_code == 404, file
    assert client.get(f"/v1/media/../reel.mp4?exp={exp}&sig={'0' * 64}").status_code == 404


def test_retired_reel_media_is_410(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, _, reel = env
    with Session(create_db_engine(settings.database_url)) as session:
        row = session.get(Reel, reel.id)
        assert row is not None
        row.expires_at = format_utc_iso(NOW - datetime.timedelta(minutes=1))
        session.add(row)
        session.commit()
    assert client.get(_media_url(settings, reel)).status_code == 410


# --- reels API -----------------------------------------------------------------------------------


def test_reel_list_detail_save_and_delete(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, token, reel = env
    h = auth_headers(token)
    listing = client.get("/v1/reels", headers=h).json()
    assert [r["public_id"] for r in listing] == [reel.public_id]
    assert listing[0]["thumbnail_url"].startswith("https://chronos.test:8443/v1/media/")
    detail = client.get(f"/v1/reels/{reel.public_id}", headers=h).json()
    assert detail["media_url"].split("?")[0].endswith(f"/{reel.public_id}/reel.mp4")
    assert detail["scenes"][1] == {"index": 1, "type": "story", "title": "A story",
                                   "source": "BBC News", "url": "https://bbc.co.uk/x",
                                   "start_sec": 3.0, "end_sec": 12.0}  # fmt: skip
    media_path = detail["media_url"].removeprefix("https://chronos.test:8443")
    assert client.get(media_path).content == MP4  # the signed URL from the API works

    saved = client.post(f"/v1/reels/{reel.public_id}/save", headers=h).json()
    assert saved["saved"] is True and saved["expires_at"] is None
    assert client.delete(f"/v1/reels/{reel.public_id}/save", headers=h).json()["saved"] is False

    assert client.delete(f"/v1/reels/{reel.public_id}", headers=h).status_code == 204
    folder = settings.data_root / "media" / "2026-10-06" / reel.public_id
    assert not folder.exists()
    assert client.get(f"/v1/reels/{reel.public_id}", headers=h).status_code == 404
    with Session(create_db_engine(settings.database_url)) as session:
        assert session.exec(select(ReelAsset)).all() == []


def test_reels_scoped_to_device_and_need_auth(env: tuple[Settings, TestClient, str, Reel]) -> None:
    settings, client, _, reel = env
    assert client.get("/v1/reels").status_code == 401
    other_token, _ = insert_device_for_test(settings)
    h = auth_headers(other_token)
    assert client.get("/v1/reels", headers=h).json() == []
    assert client.get(f"/v1/reels/{reel.public_id}", headers=h).status_code == 404
    assert client.delete(f"/v1/reels/{reel.public_id}", headers=h).status_code == 404


# --- retention (frozen clock) -------------------------------------------------------------


def test_retention_deletes_exactly_the_expected_rows_and_files(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    settings = make_settings(database_url=tmp_db_url, data_root=str(tmp_path / "data"))
    now = datetime.datetime(2026, 10, 20, 3, 0, tzinfo=datetime.UTC)
    old = now - datetime.timedelta(hours=30)
    with Session(create_db_engine(tmp_db_url)) as session:
        device = make_device(session)
        assert device.id is not None
        gone = _make_reel(session, settings, device.id, "2026-10-18", saved=0, created=old,
                          expires=old + datetime.timedelta(hours=24))  # fmt: skip
        kept_saved = _make_reel(session, settings, device.id, "2026-10-17", saved=1, created=old,
                                expires=old)  # fmt: skip
        fresh = _make_reel(session, settings, device.id, "2026-10-19", saved=0,
                           created=now - datetime.timedelta(hours=2), expires=now)  # fmt: skip
        src = Source(key="s", kind="rss", url="https://a", category="world")
        session.add(src)
        session.flush()
        stale = format_utc_iso(now - datetime.timedelta(days=20))
        orphan = Item(source_id=src.id or 0, external_id="o", url="u", url_hash="h", title="t",
                      fetched_at=stale)  # fmt: skip
        referenced = Item(source_id=src.id or 0, external_id="r", url="u", url_hash="h",
                          title="t", fetched_at=stale)  # fmt: skip
        session.add_all([orphan, referenced])
        session.flush()
        session.add(BriefItem(brief_id=kept_saved.brief_id, item_id=referenced.id or 0,
                              scene_index=1))  # fmt: skip
        session.add(Job(kind="ingest", key="old", payload_json="{}", status="done",
                        run_after=stale, finished_at=stale, created_at=stale))  # fmt: skip
        recent = format_utc_iso(now)
        session.add(Job(kind="ingest", key="new", payload_json="{}", status="done",
                        run_after=stale, finished_at=recent, created_at=stale))  # fmt: skip
        session.commit()

        report = run_retention(session, settings.data_root, now)

        reels = {r.public_id for r in session.exec(select(Reel)).all()}
        assert reels == {kept_saved.public_id, fresh.public_id}
        assert report.deleted["reels"] == 1
        media = settings.data_root / "media"
        assert not (media / "2026-10-18" / gone.public_id).exists()
        assert (media / "2026-10-17" / kept_saved.public_id / "reel.mp4").exists()
        assert (media / "2026-10-19" / fresh.public_id / "reel.mp4").exists()
        items = {i.external_id for i in session.exec(select(Item)).all()}
        assert items == {"r"}  # the orphan went, the referenced one stayed
        assert {j.key for j in session.exec(select(Job)).all()} == {"new"}


def test_backup_database_keeps_seven(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    engine = create_db_engine(tmp_db_url)
    for day in range(1, 10):
        backup_database(engine, tmp_path, datetime.datetime(2026, 10, day, 3, tzinfo=datetime.UTC))
    files = sorted((tmp_path / "backups").glob("*.sqlite"))
    assert files[0].name == "chronos-20261003.sqlite" and len(files) == 7
    assert files[-1].stat().st_size > 0


def test_scheduler_enqueues_retention_once_per_local_day(test_settings: Settings) -> None:
    from chronos.delivery.push import FakePushClient
    from chronos.scheduler import Scheduler
    from chronos.wake.rules import ensure_initial_version, get_rules_cache, reset_rules_cache

    engine = create_db_engine(test_settings.database_url)
    reset_rules_cache()
    with Session(engine) as session:
        ensure_initial_version(session, Path(__file__).parents[1] / "config" / "wake_rules.yaml")
        session.commit()
        get_rules_cache().get_effective(session)
        make_device(session, tz="Asia/Kolkata", status="active")
        session.commit()
    sched = Scheduler(session_factory=lambda: Session(engine), push_client=FakePushClient(),
                      settings=test_settings)  # fmt: skip
    t = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=datetime.UTC)  # 05:30 IST, after 03:00
    assert sched.enqueue_retention(t) == "retention:2026-10-06"
    sched.enqueue_retention(t + datetime.timedelta(minutes=1))
    with Session(engine) as session:
        assert len(session.exec(select(Job).where(Job.kind == "retention")).all()) == 1
