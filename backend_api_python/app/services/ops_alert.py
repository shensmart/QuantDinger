"""Ops alerting: tell a human when a data-collection job silently fails.

Why this exists
---------------
Before this module, a broken collector only wrote ``logger.warning``.  That is
not an alert, because nobody reads container logs.  Real examples found in this
database when the module was written:

* ``qd_market_sync_runs`` had 9 consecutive ``failed`` runs (Binance GEO-blocked
  every exchange context) and nobody noticed for days.
* ``qd_fundamental_sync_items`` had 654 ``failed`` rows.

Design
------
* One task (``ops.watchdog_tick``) scans the failure state that already exists
  in the database and pushes a digest to an IM channel.
* Delivery reuses the vendor dialect handling in
  ``app.services.notifications.webhook`` (Feishu / DingTalk / WeCom / Slack /
  generic), so there is no second webhook implementation to keep in sync.
* Channels come from environment variables so ops alerting never depends on a
  user row or the frontend's per-strategy config.
* **Only state changes are pushed.**  A digest is re-sent when the failure set
  changes or after ``OPS_ALERT_REMIND_SEC``; a persistent problem would
  otherwise spam the channel every tick and get muted.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.notifications.webhook import (
    adapt_payload_for_dialect,
    check_vendor_response,
    detect_webhook_dialect,
    dingtalk_signed_url,
    feishu_sign,
)
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

STATE_KEY = "ops_alert_state"
DEFAULT_REMIND_SEC = int(os.getenv("OPS_ALERT_REMIND_SEC", "21600") or 21600)  # 6h
DEFAULT_TIMEOUT = float(os.getenv("OPS_ALERT_TIMEOUT_SEC", "8") or 8)


# ---------------------------------------------------------------------------
# Channel configuration
# ---------------------------------------------------------------------------


def _env_list(*names: str) -> List[str]:
    out: List[str] = []
    for name in names:
        raw = str(os.getenv(name) or "").strip()
        if not raw:
            continue
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def webhook_targets() -> List[str]:
    """Webhook URLs to alert.  Supports the common aliases operators use."""
    return _env_list(
        "OPS_ALERT_WEBHOOK",
        "OPS_ALERT_WEBHOOKS",
        "ALERT_WEBHOOK_URL",
        "DINGTALK_WEBHOOK",
        "FEISHU_WEBHOOK",
        "WECOM_WEBHOOK",
        "SLACK_WEBHOOK",
    )


def telegram_targets() -> List[Tuple[str, str]]:
    """``(bot_token, chat_id)`` pairs from the environment."""
    token = str(
        os.getenv("OPS_ALERT_TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        return []
    chats = _env_list("OPS_ALERT_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")
    return [(token, c) for c in chats]


def signing_secret_for(url: str) -> Optional[str]:
    dialect = detect_webhook_dialect(url)
    if dialect == "feishu":
        return (os.getenv("OPS_ALERT_FEISHU_SECRET") or os.getenv("FEISHU_WEBHOOK_SECRET") or "").strip() or None
    if dialect == "dingtalk":
        return (os.getenv("OPS_ALERT_DINGTALK_SECRET") or os.getenv("DINGTALK_WEBHOOK_SECRET") or "").strip() or None
    return None


def alerts_configured() -> bool:
    return bool(webhook_targets() or telegram_targets())


# ---------------------------------------------------------------------------
# Failure collection
# ---------------------------------------------------------------------------


def _query(sql: str, params: Sequence[Any] = (), many: bool = True):
    """Run a statement and fetch rows only when it can return them.

    Non-SELECT statements have no result set, so calling ``fetchone`` on them
    raises ProgrammingError.  ``RETURNING`` is checked explicitly because those
    statements do produce rows.
    """
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(sql, params)
            head = sql.lstrip().upper()
            returns_rows = head.startswith("SELECT") or "RETURNING" in head
            rows = (cur.fetchall() if many else cur.fetchone()) if returns_rows else None
            db.commit()
            return rows
        finally:
            cur.close()


def _get(row: Any, key: str, idx: int = 0):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        try:
            return row[idx]
        except Exception:
            return None


def _business_days_between(start: datetime, end: datetime) -> int:
    """Count Mon-Fri days strictly between the two instants (weekend-tolerant).

    A raw hour threshold would fire every weekend: Friday's close to Monday's
    open is ~66h with no data expected.  Counting business days instead keeps
    the signal honest without needing the (itself stale) exchange calendar.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start.date()
    end_day = end.date()
    while cursor < end_day:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def stale_collectors() -> List[Dict[str, Any]]:
    """Detect collectors that are silently producing nothing.

    Status-based checks miss the worst failure mode: a task that runs fine but
    writes no rows.  Real case found while writing this module — the HiThink
    event sync sat on ``{'skipped': 'not_due'}`` for 13 days because the
    upstream trading calendar had gone stale, and nothing surfaced it.

    Threshold is expressed in *business days* so a weekend or a single holiday
    does not trip it.
    """
    # (table, timestamp column, business days allowed before alerting)
    checks = (
        ("qd_fundamental_snapshots", "ingested_at", 2),
        ("qd_symbol_quote_snapshots", "updated_at", 2),
        ("qd_market_events", "ingested_at", 3),
    )
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for table, column, max_days in checks:
        try:
            rows = _query(
                "SELECT max(%s) AS last_write FROM %s" % (column, table),
                (),
            ) or []
        except Exception as exc:
            logger.debug("freshness check %s skipped: %s", table, exc)
            continue
        last = _get(rows[0], "last_write", 0) if rows else None
        if last is None:
            out.append(
                {
                    "check": "stale_%s" % table,
                    "label": "数据源无数据",
                    "count": 1,
                    "detail": "%s 从未写入过数据" % table,
                }
            )
            continue
        if isinstance(last, datetime) and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if not isinstance(last, datetime):
            continue
        missed = _business_days_between(last, now)
        if missed > max_days:
            out.append(
                {
                    "check": "stale_%s" % table,
                    "label": "数据源已停更",
                    "count": 1,
                    "detail": "%s 已错过 %d 个工作日（阈值 %d，最后写入 %s）"
                    % (table, missed, max_days, last.strftime("%m-%d %H:%M")),
                }
            )
    return out


def collect_failures(window_hours: int = 24) -> List[Dict[str, Any]]:
    """Scan every collector that records its own failure state.

    Each check is independent: a schema change in one table must not stop the
    watchdog from reporting on the others.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    found: List[Dict[str, Any]] = []

    checks: Tuple[Tuple[str, str, str], ...] = (
        (
            "market_sync",
            "SELECT id, trigger_type, started_at, result FROM qd_market_sync_runs "
            "WHERE status = 'failed' AND started_at >= %s ORDER BY id DESC LIMIT 5",
            "行情目录同步失败",
        ),
        (
            "market_event_sync",
            "SELECT id, source, started_at, detail_json FROM qd_market_event_sync_runs "
            "WHERE status = 'failed' AND started_at >= %s ORDER BY id DESC LIMIT 5",
            "事件库同步失败",
        ),
        (
            "fundamental_sync",
            "SELECT count(*) AS n FROM qd_fundamental_sync_items "
            "WHERE status = 'failed' AND updated_at >= %s",
            "基本面采集失败",
        ),
        (
            "chip_sync",
            "SELECT count(*) AS n FROM qd_chip_sync_items "
            "WHERE status = 'failed' AND updated_at >= %s",
            "筹码采集失败",
        ),
    )

    for name, sql, label in checks:
        try:
            rows = _query(sql, (since,)) or []
        except Exception as exc:
            logger.debug("ops alert check %s skipped: %s", name, exc)
            continue

        if name in {"fundamental_sync", "chip_sync"}:
            count = int(_get(rows[0], "n", 0) or 0) if rows else 0
            if count:
                found.append(
                    {
                        "check": name,
                        "label": label,
                        "count": count,
                        "detail": "%d 个标的采集失败（近 %dh）" % (count, window_hours),
                    }
                )
            continue

        for r in rows:
            detail = _get(r, "result", 3) or _get(r, "detail_json", 3) or ""
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            found.append(
                {
                    "check": name,
                    "label": label,
                    "count": 1,
                    "detail": "%s #%s %s" % (
                        _get(r, "trigger_type", 1) or _get(r, "source", 1) or "",
                        _get(r, "id", 0),
                        _first_error(detail),
                    ),
                }
            )

    return found


def _first_error(raw: str) -> str:
    """Pull the first human-useful error line out of a JSON blob or text."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        return text[:160]
    if isinstance(obj, dict):
        for ctx in obj.get("contexts") or []:
            if isinstance(ctx, dict) and ctx.get("error"):
                return ("%s: %s" % (ctx.get("provider", ""), ctx.get("error"))).strip()[:160]
        if obj.get("error"):
            return str(obj["error"])[:160]
    return text[:160]


def stale_workers(window_minutes: int = 15) -> List[Dict[str, Any]]:
    """Roles whose heartbeat is older than ``window_minutes``.

    Only the newest row per role counts.  Containers get recreated with a new
    ``worker_id``, and the old row stays behind with ``status='running'``; a
    naive scan of every row reports those ghosts as dead workers forever.
    Rows explicitly marked ``stopped`` are ignored too.
    """
    try:
        rows = _query(
            """
            SELECT worker_id, role, heartbeat_at, status FROM (
                SELECT DISTINCT ON (role) worker_id, role, heartbeat_at, status
                FROM qd_worker_heartbeats
                ORDER BY role, heartbeat_at DESC
            ) latest
            WHERE status = 'running'
              AND heartbeat_at < NOW() - (%s * INTERVAL '1 minute')
            """,
            (int(window_minutes),),
        ) or []
    except Exception as exc:
        logger.debug("ops alert worker check skipped: %s", exc)
        return []
    out = []
    for r in rows:
        hb = _get(r, "heartbeat_at", 2)
        if isinstance(hb, datetime) and hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age_min = (
            (datetime.now(timezone.utc) - hb).total_seconds() / 60.0
            if isinstance(hb, datetime)
            else -1.0
        )
        out.append(
            {
                "check": "worker_heartbeat",
                "label": "进程心跳超时",
                "count": 1,
                "detail": "%s (%s) 已 %.0f 分钟无心跳" % (
                    _get(r, "role", 1),
                    _get(r, "worker_id", 0),
                    age_min,
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Digest state (de-duplication)
# ---------------------------------------------------------------------------


def _ensure_state_table() -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS qd_ops_alert_state (
                    key VARCHAR(64) PRIMARY KEY,
                    fingerprint VARCHAR(64) NOT NULL DEFAULT '',
                    last_sent_at TIMESTAMPTZ,
                    last_result VARCHAR(200) NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            db.commit()
        finally:
            cur.close()


def _fingerprint(items: Sequence[Dict[str, Any]]) -> str:
    import hashlib

    blob = json.dumps(sorted((i["check"], i["label"], i["detail"]) for i in items), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _load_state() -> Dict[str, Any]:
    try:
        rows = _query(
            "SELECT fingerprint, last_sent_at FROM qd_ops_alert_state WHERE key = %s", (STATE_KEY,)
        ) or []
    except Exception:
        return {}
    if not rows:
        return {}
    return {
        "fingerprint": _get(rows[0], "fingerprint", 0) or "",
        "last_sent_at": _get(rows[0], "last_sent_at", 1),
    }


def _save_state(fingerprint: str, last_sent_at: Optional[datetime], result: str) -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                INSERT INTO qd_ops_alert_state (key, fingerprint, last_sent_at, last_result, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    fingerprint = EXCLUDED.fingerprint,
                    last_sent_at = COALESCE(EXCLUDED.last_sent_at, qd_ops_alert_state.last_sent_at),
                    last_result = EXCLUDED.last_result,
                    updated_at = NOW()
                """,
                (STATE_KEY, fingerprint, last_sent_at, result[:200]),
            )
            db.commit()
        finally:
            cur.close()


def should_send(items: Sequence[Dict[str, Any]], state: Dict[str, Any], remind_sec: int) -> Tuple[bool, str]:
    """Decide whether this digest is worth sending.

    Returns ``(send, reason)``.  Sending on every tick would train the operator
    to mute the channel, which is worse than not alerting at all.
    """
    if not items:
        # Only notify a recovery if we previously reported a problem.
        if state.get("fingerprint"):
            return True, "recovered"
        return False, "nothing_to_report"

    fp = _fingerprint(items)
    previous = state.get("fingerprint") or ""
    if fp != previous:
        # No prior fingerprint means this is the first report, not a change.
        return True, ("changed" if previous else "first")

    last = state.get("last_sent_at")
    if last is None:
        return True, "first"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age >= remind_sec:
        return True, "reminder"
    return False, "suppressed"


# ---------------------------------------------------------------------------
# Rendering + delivery
# ---------------------------------------------------------------------------


def render_digest(items: Sequence[Dict[str, Any]], reason: str) -> Tuple[str, str]:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    if not items:
        return "QuantDinger 采集恢复正常", "所有采集任务已恢复正常。\n时间：%s" % now

    total = sum(int(i.get("count") or 1) for i in items)
    title = "QuantDinger 采集告警（%d 项）" % len(items)
    lines = ["检测时间：%s" % now, "受影响项：%d" % total, ""]
    for i in items:
        lines.append("- 【%s】%s" % (i["label"], i["detail"]))
    lines.append("")
    lines.append("来源：ops watchdog（%s）" % reason)
    return title, "\n".join(lines)


def _post_webhook(url: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    import requests

    dialect = detect_webhook_dialect(url)
    body = adapt_payload_for_dialect(dialect, payload)
    headers = {"Content-Type": "application/json", "User-Agent": "QuantDinger-OpsAlert/1.0"}

    target = url
    secret = signing_secret_for(url)
    if dialect == "feishu" and secret:
        ts = str(int(time.time()))
        body = dict(body)
        body["timestamp"] = ts
        body["sign"] = feishu_sign(secret, ts)
    elif dialect == "dingtalk" and secret:
        target = dingtalk_signed_url(url, secret)

    try:
        resp = requests.post(target, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)

    ok, err = check_vendor_response(dialect, resp.status_code, resp.text)
    if ok:
        return True, ""
    # One retry on transient failures, mirroring the signal notifier.
    if resp.status_code == 429 or resp.status_code >= 500:
        time.sleep(1.5)
        try:
            resp = requests.post(target, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            return check_vendor_response(dialect, resp.status_code, resp.text)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
    return False, err


def _post_telegram(token: str, chat_id: str, title: str, body: str) -> Tuple[bool, str]:
    import requests

    try:
        resp = requests.post(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            json={"chat_id": chat_id, "text": "%s\n\n%s" % (title, body), "disable_web_page_preview": True},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    if resp.status_code // 100 != 2:
        return False, "http_%s:%s" % (resp.status_code, resp.text[:200])
    try:
        if not resp.json().get("ok", False):
            return False, "telegram_not_ok"
    except Exception:
        pass
    return True, ""


def deliver(title: str, body: str) -> Tuple[bool, str]:
    """Send to every configured channel.  Returns (any_success, summary)."""
    payload = {"title": title, "message": body}
    results: List[str] = []
    any_ok = False

    for url in webhook_targets():
        ok, err = _post_webhook(url, payload)
        any_ok = any_ok or ok
        results.append("%s:%s" % (detect_webhook_dialect(url), "ok" if ok else err))

    for token, chat in telegram_targets():
        ok, err = _post_telegram(token, chat, title, body)
        any_ok = any_ok or ok
        results.append("telegram:%s" % ("ok" if ok else err))

    if not results:
        return False, "no_channels_configured"
    return any_ok, "; ".join(results)[:200]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_watchdog(*, force: bool = False, window_hours: int = 24) -> Dict[str, Any]:
    """Scan for failures and alert if warranted.  Never raises."""
    _ensure_state_table()

    items = collect_failures(window_hours) + stale_workers() + stale_collectors()
    state = _load_state()
    send, reason = should_send(items, state, DEFAULT_REMIND_SEC) if not force else (True, "forced")

    if not send:
        return {"sent": False, "reason": reason, "items": len(items)}

    title, body = render_digest(items, reason)
    if not alerts_configured():
        logger.warning(
            "Ops alert triggered (%s, %d items) but no channel is configured. "
            "Set OPS_ALERT_WEBHOOK (Feishu/DingTalk/WeCom/Slack) or "
            "OPS_ALERT_TELEGRAM_BOT_TOKEN + OPS_ALERT_TELEGRAM_CHAT_ID.",
            reason,
            len(items),
        )
        return {"sent": False, "reason": "no_channels", "items": len(items), "title": title}

    ok, detail = deliver(title, body)
    fp = _fingerprint(items) if items else ""
    # Clear the fingerprint on recovery so the next incident alerts immediately.
    _save_state(fp, datetime.now(timezone.utc) if ok else None, detail)
    if ok:
        logger.info("Ops alert sent (%s, %d items): %s", reason, len(items), detail)
    else:
        logger.error("Ops alert delivery failed (%s): %s", reason, detail)
    return {"sent": ok, "reason": reason, "items": len(items), "delivery": detail}


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse

    ap = argparse.ArgumentParser(description="Ops watchdog for data collectors")
    ap.add_argument("--force", action="store_true", help="send even if unchanged")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true", help="print the digest, send nothing")
    args = ap.parse_args()

    if args.dry_run:
        found = collect_failures(args.window_hours) + stale_workers() + stale_collectors()
        t, b = render_digest(found, "dry-run")
        print("=== %s ===" % t)
        print(b)
        print()
        print("channels configured:", alerts_configured(), webhook_targets())
    else:
        print(run_watchdog(force=args.force, window_hours=args.window_hours))
