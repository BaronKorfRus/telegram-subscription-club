from datetime import date

import httpx
import pytest
import respx
from app import migrate_salebot
from app.migrate_salebot import (
    apply_rows,
    build_report,
    fetch_clients,
    main,
    map_client,
    read_ids,
)
from app.models import Client, Subscription, SubscriptionStatus
from sqlalchemy import func, select, text

TODAY = date(2026, 10, 23)
BASE = "https://salebot.test/api"
KEY = "test-key"

SAMPLE = {
    "platform_id": "1001",
    "name": "Анна Казакова",
    "phone": "+79991234567",
    "email": "Anna@Example.RU",
    "tarif": 1,
    "datespis": "01.10.2026",
    "tinkoff_rebill_id": "rebill-1",
}


def test_mapping_legacy_and_paused():
    row = map_client(SAMPLE, today=TODAY)

    assert row is not None
    assert row.tariff_code == "m1_legacy"
    assert row.billing_enabled is False
    assert row.status == "past_due"
    assert row.email == "anna@example.ru"
    assert row.first_name == "Анна"
    assert row.last_name == "Казакова"


def test_mapping_period_end_today_is_paused_in_october():
    row = map_client({**SAMPLE, "datespis": "23.10.2026"}, today=TODAY)

    assert row is not None
    assert row.status == SubscriptionStatus.past_due.value
    assert row.billing_enabled is False


def test_mapping_future_period_end_is_active():
    row = map_client({**SAMPLE, "datespis": "22.11.2026"}, today=TODAY)

    assert row is not None
    assert row.status == SubscriptionStatus.active.value
    assert row.billing_enabled is True


@pytest.mark.parametrize(
    ("tarif", "code"),
    [(1, "m1_legacy"), (2, "m3_legacy"), (3, "m6_legacy"), (4, "m1"), (5, "m3"), (6, "m6")],
)
def test_tariff_mapping(tarif, code):
    row = map_client({**SAMPLE, "tarif": tarif, "datespis": "22.11.2026"}, today=TODAY)
    assert row is not None and row.tariff_code == code


@pytest.mark.parametrize("datespis", ["01.09.2026", "01.12.2025", ""])
def test_old_or_invalid_dates_are_skipped(datespis):
    assert map_client({**SAMPLE, "datespis": datespis}, today=TODAY) is None


@respx.mock
def test_fetch_clients_merges_variables(monkeypatch):
    monkeypatch.setenv("SALEBOT_API_KEY", KEY)
    monkeypatch.setenv("SALEBOT_API_BASE", BASE)
    clients_route = respx.get(f"{BASE}/{KEY}/get_clients?limit=500").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "clients": [
                    {
                        "id": 11,
                        "platform_id": "1001",
                        "name": "Анна",
                        "variables": {"tg_username": "@anna"},
                    }
                ],
            },
        )
    )
    respx.get(f"{BASE}/{KEY}/get_variables?client_id=11").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "result": {
                    "tarif": "1",
                    "datespis": "01.10.2026",
                    "phone": "+79991234567",
                    "tinkoff_rebill_id": "rebill-1",
                },
            },
        )
    )

    result = fetch_clients(["11"])

    assert clients_route.called
    assert result.not_found == []
    assert result.errors == []
    assert result.clients[0]["variables"]["tg_username"] == "@anna"
    assert result.clients[0]["variables"]["tarif"] == "1"


@respx.mock
def test_fetch_clients_reports_missing_client_id(monkeypatch):
    monkeypatch.setenv("SALEBOT_API_KEY", KEY)
    monkeypatch.setenv("SALEBOT_API_BASE", BASE)
    respx.get(f"{BASE}/{KEY}/get_clients?limit=500").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "clients": [{"id": 11, "platform_id": "1001", "name": "Анна"}],
            },
        )
    )
    respx.get(f"{BASE}/{KEY}/get_variables?client_id=11").mock(
        return_value=httpx.Response(200, json={"status": "success", "result": {}})
    )

    result = fetch_clients(["11", "999"])

    assert result.not_found == ["999"]
    assert len(result.clients) == 1


@respx.mock
def test_one_variables_error_does_not_abort_others(monkeypatch):
    monkeypatch.setenv("SALEBOT_API_KEY", KEY)
    monkeypatch.setenv("SALEBOT_API_BASE", BASE)
    respx.get(f"{BASE}/{KEY}/get_clients?limit=500").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "clients": [
                    {"id": 11, "platform_id": "1001", "name": "Анна"},
                    {"id": 12, "platform_id": "1002", "name": "Мария"},
                ],
            },
        )
    )
    respx.get(f"{BASE}/{KEY}/get_variables?client_id=11").mock(
        return_value=httpx.Response(200, json={"status": "success", "result": {}})
    )
    respx.get(f"{BASE}/{KEY}/get_variables?client_id=12").mock(return_value=httpx.Response(500))

    result = fetch_clients(["11", "12"])

    assert len(result.clients) == 2
    assert len(result.errors) == 1
    assert "12" in result.errors[0]


def test_apply_is_idempotent(session):
    rows = [map_client(SAMPLE, today=TODAY)]
    assert rows[0] is not None
    rows = [row for row in rows if row]

    assert apply_rows(session, rows) == 1
    assert apply_rows(session, rows) == 1

    assert session.scalar(select(func.count()).select_from(Client)) == 1
    assert session.scalar(select(func.count()).select_from(Subscription)) == 1
    client = session.scalar(select(Client))
    subscription = session.scalar(select(Subscription))
    assert client.tg_id == 1001
    assert subscription.tariff.code == "m1_legacy"
    assert subscription.billing_enabled is False
    assert subscription.status == SubscriptionStatus.past_due


def test_report_lists_missing_fields():
    row = map_client({**SAMPLE, "phone": "", "email": "", "tinkoff_rebill_id": ""}, today=TODAY)
    assert row is not None
    result = migrate_salebot.FetchResult(
        clients=[SAMPLE], not_found=["999"], errors=["12: timeout"]
    )
    report = build_report(3, result, [row])

    text = report.render()
    assert "Найдено: 1" in text
    assert "Перенесётся: 1" in text
    assert "Не найдено client_id: 999" in text
    assert "Без rebill: 1001" in text
    assert "Без телефона: 1001" in text
    assert "Без email: 1001" in text
    assert "Ошибки get_variables:" in text
    assert "12: timeout" in text


def test_dry_run_does_not_write(session, monkeypatch, tmp_path, capsys):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("1001\n")
    monkeypatch.setattr(migrate_salebot, "_today", lambda: TODAY)
    monkeypatch.setattr(
        migrate_salebot, "fetch_clients", lambda ids: migrate_salebot.FetchResult(clients=[SAMPLE])
    )

    assert main(["--ids", str(ids_file), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Найдено: 1" in out
    assert "Перенесётся: 1" in out
    assert session.scalar(select(func.count()).select_from(Client)) == 0
    assert session.scalar(select(func.count()).select_from(Subscription)) == 0


def test_read_ids_skips_empty_lines(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("1001\n\n# comment\n1002\n")
    assert read_ids(str(path)) == ["1001", "1002"]


def test_cli_apply_is_idempotent(session, monkeypatch, tmp_path, capsys):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("1001\n")
    monkeypatch.setattr(migrate_salebot, "_today", lambda: TODAY)
    monkeypatch.setattr(
        migrate_salebot, "fetch_clients", lambda ids: migrate_salebot.FetchResult(clients=[SAMPLE])
    )
    try:
        assert main(["--ids", str(ids_file), "--apply"]) == 0
        assert main(["--ids", str(ids_file), "--apply"]) == 0
        session.expire_all()
        assert session.scalar(select(func.count()).select_from(Client)) == 1
        assert session.scalar(select(func.count()).select_from(Subscription)) == 1
        assert "Перенесено: 1" in capsys.readouterr().out
    finally:
        session.rollback()
        session.execute(text("TRUNCATE subscriptions, clients, tariffs RESTART IDENTITY CASCADE"))
        session.commit()


def test_httpx_logger_is_warning():
    import logging

    assert logging.getLogger("httpx").level == logging.WARNING
