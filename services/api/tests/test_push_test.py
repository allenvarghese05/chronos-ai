"""Visible tests are bounded, device-bound and not wake ticks."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from chronos.api.admin import test_push_client as push_client_dependency
from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.delivery.push import FakePushClient, PushMessage, Ticket
from chronos.main import create_app
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.wake import Day, WakeEvaluation
from tests.test_config_wake import auth_headers, insert_device_for_test


def fixture_client(
    settings: Settings, fake: FakePushClient, registered: bool = True
) -> tuple[TestClient, str]:
    token, device = insert_device_for_test(settings)
    if registered:
        with Session(create_db_engine(settings.database_url)) as session:
            row = session.get(Device, device.id)
            assert row is not None
            row.expo_push_token = "ExponentPushToken[isolated-test-only]"
            session.add(row)
            session.commit()
    app = create_app(settings)
    app.dependency_overrides[push_client_dependency] = lambda: fake
    return TestClient(app), token


def test_visible_push_audit_and_no_wake_mutation(test_settings: Settings) -> None:
    fake = FakePushClient()
    client, token = fixture_client(test_settings, fake)
    with client:
        response = client.post("/v1/admin/push-test", headers=auth_headers(token))
        assert response.json() == {"outcome": "accepted", "reason": "ticket_accepted"}
        assert "isolated-test-only" not in response.text
        payload = fake.sent[0].to_expo_dict()
        assert payload["_contentAvailable"] is False
        assert payload["sound"] == "default" and payload["title"] and payload["body"]
        assert payload["data"] == {"type": "test"}
        assert client.post("/v1/admin/push-test", headers=auth_headers(token)).status_code == 429
    with Session(create_db_engine(test_settings.database_url)) as session:
        audit = session.exec(select(PushNotification)).one()
        assert audit.kind == "test" and audit.tick_seq is None
        assert "isolated-test-only" not in audit.payload_json
        assert not session.exec(select(Day)).all()
        assert not session.exec(select(WakeEvaluation)).all()


def test_no_token_no_external_io(test_settings: Settings) -> None:
    fake = FakePushClient()
    client, token = fixture_client(test_settings, fake, registered=False)
    with client:
        assert client.post("/v1/admin/push-test").status_code == 401
        assert (
            client.post("/v1/admin/push-test", headers=auth_headers(token)).json()["outcome"]
            == "unavailable"
        )
    assert fake.sent == []


def test_arbitrary_payload_not_accepted(test_settings: Settings) -> None:
    fake = FakePushClient()
    client, token = fixture_client(test_settings, fake)
    with client:
        assert (
            client.post(
                "/v1/admin/push-test", headers=auth_headers(token), json={"to": "elsewhere"}
            ).status_code
            == 422
        )
    assert fake.sent == []


def test_rejection_sanitized_token_invalidated_without_revoking_pairing(
    test_settings: Settings,
) -> None:
    fake = FakePushClient(outcomes=["DeviceNotRegistered"])
    client, token = fixture_client(test_settings, fake)
    with client:
        assert client.post("/v1/admin/push-test", headers=auth_headers(token)).json() == {
            "outcome": "failed",
            "reason": "provider_rejected",
        }
    with Session(create_db_engine(test_settings.database_url)) as session:
        row = session.exec(select(Device)).one()
        assert row.expo_push_token is None and row.status == "active"


def test_timeout_is_unknown_and_never_retried(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chronos.api.admin as admin

    class Stalled(FakePushClient):
        attempts = 0

        async def send_once(self, messages: Sequence[PushMessage]) -> list[Ticket]:
            self.attempts += 1
            await asyncio.sleep(1)
            return []

    fake = Stalled()
    client, token = fixture_client(test_settings, fake)
    monkeypatch.setattr(admin, "TEST_TIMEOUT_SECONDS", 0.01)
    with client:
        assert client.post("/v1/admin/push-test", headers=auth_headers(token)).json() == {
            "outcome": "unknown",
            "reason": "uncertain",
        }
    assert fake.attempts == 1
