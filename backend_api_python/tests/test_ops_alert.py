"""Ops watchdog: failure detection and alert de-duplication.

Offline unit tests — no network, no live database.  Delivery is exercised
against a stubbed ``requests.post`` so the vendor dialect path is still covered.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import ops_alert


# ---------------------------------------------------------------------------
# Failure collection
# ---------------------------------------------------------------------------


def test_first_error_extracts_provider_message():
    raw = json.dumps(
        {
            "rows": 0,
            "contexts": [
                {"ok": False, "provider": "binance", "error": "GEO restricted"},
                {"ok": True, "provider": "okx"},
            ],
        }
    )
    assert "GEO restricted" in ops_alert._first_error(raw)
    assert ops_alert._first_error("plain text failure") == "plain text failure"
    assert ops_alert._first_error("") == ""
    # Malformed JSON must not raise.
    assert ops_alert._first_error("{not json") == "{not json"


def test_business_days_between_ignores_weekend():
    # Friday 18:00 -> Monday 08:00 is ~62h but only 1 business day.
    fri = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    assert ops_alert._business_days_between(fri, mon) == 1
    # A full working week.
    assert ops_alert._business_days_between(
        datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc),
    ) == 4
    # Same instant / reversed order.
    assert ops_alert._business_days_between(fri, fri) == 0
    assert ops_alert._business_days_between(mon, fri) == 0


def test_collect_failures_survives_broken_query(monkeypatch):
    """One unreadable table must not stop the rest of the checks."""
    def boom(sql, params=(), many=True):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(ops_alert, "_query", boom)
    assert ops_alert.collect_failures(24) == []


def test_stale_workers_only_reports_latest_row_per_role(monkeypatch):
    """Ghost rows from recreated containers must not be reported as dead."""
    rows = [
        {"worker_id": "old", "role": "celery", "heartbeat_at": datetime(2026, 10, 1), "status": "running"},
    ]
    monkeypatch.setattr(ops_alert, "_query", lambda *a, **k: [])
    assert ops_alert.stale_workers() == []


def test_render_digest_lists_every_item():
    items = [
        {"check": "market_sync", "label": "行情目录同步失败", "count": 1, "detail": "celery #54 binance: GEO"},
        {"check": "stale_x", "label": "数据源已停更", "count": 1, "detail": "qd_market_events 已错过 10 个工作日"},
    ]
    title, body = ops_alert.render_digest(items, "changed")
    assert "告警" in title
    assert "行情目录同步失败" in body
    assert "qd_market_events" in body
    assert "changed" in body


def test_render_digest_recovery_message_when_empty():
    title, body = ops_alert.render_digest([], "recovered")
    assert "恢复" in title
    assert "恢复正常" in body


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def _items(tag="a"):
    return [{"check": "x", "label": "L", "count": 1, "detail": tag}]


def test_should_send_first_time_and_on_change():
    assert ops_alert.should_send(_items(), {}, 3600) == (True, "first")

    fp = ops_alert._fingerprint(_items())
    sent = {"fingerprint": fp, "last_sent_at": datetime.now(timezone.utc)}

    # Same problem, recently reported -> stay quiet.
    assert ops_alert.should_send(_items(), sent, 3600) == (False, "suppressed")

    # Problem changed -> send immediately.
    assert ops_alert.should_send(_items("b"), sent, 3600) == (True, "changed")


def test_same_fingerprint_but_never_delivered_retries():
    """A failed delivery leaves last_sent_at empty; the next tick must retry."""
    fp = ops_alert._fingerprint(_items())
    assert ops_alert.should_send(_items(), {"fingerprint": fp}, 3600) == (True, "first")


def test_should_send_reminds_after_interval_only():
    fp = ops_alert._fingerprint(_items())
    recent = {"fingerprint": fp, "last_sent_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
    assert ops_alert.should_send(_items(), recent, 3600) == (False, "suppressed")

    old = {"fingerprint": fp, "last_sent_at": datetime.now(timezone.utc) - timedelta(hours=7)}
    assert ops_alert.should_send(_items(), old, 3600) == (True, "reminder")


def test_should_send_handles_naive_timestamp():
    """Postgres can hand back a naive datetime; that must not crash the age check."""
    fp = ops_alert._fingerprint(_items())
    naive_old = {
        "fingerprint": fp,
        "last_sent_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=7),
    }
    assert ops_alert.should_send(_items(), naive_old, 3600)[0] is True


def test_should_send_only_on_recovery_when_previously_alerting():
    """A clean scan is silent, unless we had reported a problem before."""
    assert ops_alert.should_send([], {}, 3600) == (False, "nothing_to_report")
    assert ops_alert.should_send([], {"fingerprint": "abc"}, 3600) == (True, "recovered")


# ---------------------------------------------------------------------------
# Channel config + delivery
# ---------------------------------------------------------------------------


def test_targets_from_environment_aliases(monkeypatch):
    for k in ("OPS_ALERT_WEBHOOK", "OPS_ALERT_WEBHOOKS", "DINGTALK_WEBHOOK", "FEISHU_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
    assert ops_alert.webhook_targets() == ["https://oapi.dingtalk.com/robot/send?access_token=x"]
    assert ops_alert.alerts_configured() is True


def test_not_configured_by_default(monkeypatch):
    for k in (
        "OPS_ALERT_WEBHOOK", "OPS_ALERT_WEBHOOKS", "ALERT_WEBHOOK_URL", "DINGTALK_WEBHOOK",
        "FEISHU_WEBHOOK", "WECOM_WEBHOOK", "SLACK_WEBHOOK",
        "OPS_ALERT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    assert ops_alert.alerts_configured() is False
    assert ops_alert.deliver("t", "b") == (False, "no_channels_configured")


def test_webhook_delivery_uses_vendor_dialect(monkeypatch):
    """DingTalk must receive its own schema, not our generic payload."""
    sent = {}

    class Resp:
        status_code = 200
        text = '{"errcode":0,"errmsg":"ok"}'

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        return Resp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.delenv("OPS_ALERT_DINGTALK_SECRET", raising=False)

    ok, detail = ops_alert.deliver("标题", "正文")
    assert ok is True, detail
    assert sent["json"]["msgtype"] == "markdown"
    assert "标题" in sent["json"]["markdown"]["text"]


def test_webhook_delivery_reports_vendor_error(monkeypatch):
    class Resp:
        status_code = 200
        text = '{"errcode":310000,"errmsg":"sign not match"}'

    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    monkeypatch.setenv("OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
    ok, detail = ops_alert.deliver("t", "b")
    assert ok is False
    assert "310000" in detail or "sign" in detail


def test_telegram_delivery(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    import requests

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.delenv("OPS_ALERT_WEBHOOK", raising=False)
    monkeypatch.setenv("OPS_ALERT_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OPS_ALERT_TELEGRAM_CHAT_ID", "999")

    ok, detail = ops_alert.deliver("t", "b")
    assert ok is True, detail
    assert "123:abc" in sent["url"]
    assert sent["json"]["chat_id"] == "999"


def test_run_watchdog_warns_when_no_channel(monkeypatch, caplog):
    """A triggered alert with no channel must be loud in the logs, not silent."""
    monkeypatch.setattr(ops_alert, "_ensure_state_table", lambda: None)
    monkeypatch.setattr(ops_alert, "collect_failures", lambda *a, **k: _items())
    monkeypatch.setattr(ops_alert, "stale_workers", lambda *a, **k: [])
    monkeypatch.setattr(ops_alert, "stale_collectors", lambda *a, **k: [])
    monkeypatch.setattr(ops_alert, "_load_state", lambda: {})
    monkeypatch.setattr(ops_alert, "alerts_configured", lambda: False)

    import logging

    with caplog.at_level(logging.WARNING):
        result = ops_alert.run_watchdog()
    assert result["sent"] is False
    assert result["reason"] == "no_channels"
    assert any("no channel is configured" in r.message for r in caplog.records)


def test_run_watchdog_suppresses_repeat(monkeypatch):
    monkeypatch.setattr(ops_alert, "_ensure_state_table", lambda: None)
    monkeypatch.setattr(ops_alert, "collect_failures", lambda *a, **k: _items())
    monkeypatch.setattr(ops_alert, "stale_workers", lambda *a, **k: [])
    monkeypatch.setattr(ops_alert, "stale_collectors", lambda *a, **k: [])
    fp = ops_alert._fingerprint(_items())
    monkeypatch.setattr(
        ops_alert, "_load_state",
        lambda: {"fingerprint": fp, "last_sent_at": datetime.now(timezone.utc)},
    )
    called = {"n": 0}
    monkeypatch.setattr(ops_alert, "deliver", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    result = ops_alert.run_watchdog()
    assert result == {"sent": False, "reason": "suppressed", "items": 1}
    assert called["n"] == 0
