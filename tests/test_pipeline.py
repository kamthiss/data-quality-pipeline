from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dqpipe import run as run_module
from dqpipe.config import TZ
from dqpipe.freshness import freshness
from dqpipe.run import run
from dqpipe.source.api import app
from dqpipe.source.generator import OUT

DAY = date(2026, 8, 3)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pauses():
    return []


def set_state(client, day, fault="clean", now=None):
    now = now or datetime.combine(day + timedelta(days=1), time(3), ZoneInfo(TZ))
    r = client.post("/admin/state", json={"now": now.isoformat(), "fault": fault,
                                          "fault_date": day.isoformat()})
    assert r.status_code == 200


def load(conn, client, pauses, day=DAY, fault="clean", now=None):
    set_state(client, day, fault, now)
    return run(day, conn, client, sleep=pauses.append)


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def digest(conn, table, key):
    return one(conn, f"select md5(string_agg(t::text, '|' order by {key})) from {table} t")


def snapshot(conn):
    return digest(conn, "staging.orders", "order_id"), digest(conn, "mart.daily_revenue", "report_date, city")


def last_status(conn):
    return one(conn, "select status from meta.load_runs order by load_id desc limit 1")


def summary(conn, day):
    return one(conn, "select (payload->>'orders')::int from raw.api_summaries "
                     "where request_date = %s order by load_id desc limit 1", day)


def test_one_day(conn, client, pauses):
    first = date(2026, 8, 1)
    assert load(conn, client, pauses, first) == 0
    assert one(conn, "select count(*) from staging.orders") == summary(conn, first) > 0
    assert one(conn, "select sum(orders_created) from mart.daily_revenue") == summary(conn, first)
    assert last_status(conn) == "published"
    assert pauses == []


def test_rerun_is_idempotent(conn, client, pauses):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    pages = one(conn, "select count(*) from raw.api_pages")
    assert load(conn, client, pauses) == 0
    assert snapshot(conn) == before
    assert one(conn, "select count(*) from raw.api_pages") == 2 * pages
    assert one(conn, "select count(distinct load_id) from raw.api_pages") == 2


def test_api_500_retries(conn, client, pauses):
    assert load(conn, client, pauses, fault="api_500") == 0
    assert pauses == [1, 2]
    assert last_status(conn) == "published"


def test_api_down_keeps_state(conn, client, pauses):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    assert load(conn, client, pauses, fault="api_down") == 2
    assert pauses == [1, 2, 4]
    assert snapshot(conn) == before
    assert last_status(conn) == "failed_source"


def test_duplicates_collapsed(conn, client, pauses):
    assert load(conn, client, pauses, fault="duplicates") == 0
    n = one(conn, "select count(*) from staging.orders where report_date = %s", DAY)
    assert n == summary(conn, DAY)
    assert one(conn, "select count(*) from staging.orders_candidate where request_date = %s", DAY) > n


def test_refund_replaces_version(conn, client, pauses):
    df = pd.read_parquet(OUT)
    v2 = df[df["version"] == 2].iloc[0]
    day = v2["created_at"].tz_convert(TZ).date()
    revenue = "select sum(revenue_rub) from mart.daily_revenue where report_date = %s"
    status = "select status from staging.orders where order_id = %s"

    assert load(conn, client, pauses, day, now=v2["available_at"] - timedelta(seconds=1)) == 0
    assert one(conn, status, v2["order_id"]) == "paid"
    paid = one(conn, revenue, day)
    assert load(conn, client, pauses, day, now=v2["available_at"]) == 0
    assert one(conn, status, v2["order_id"]) == "refunded"
    assert one(conn, revenue, day) == paid - Decimal(f"{v2['amount_rub']:.2f}")


class Tampered:
    """Подменяет строки первой страницы /orders функцией change."""

    def __init__(self, client, change):
        self.client = client
        self.change = change

    def get(self, path, params):
        r = self.client.get(path, params=params)
        if path != "/orders" or params["page"] != 1:
            return r
        body = r.json()
        self.change(body["items"])
        return httpx.Response(200, json=body)


class Broken:
    def get(self, path, params):
        raise RuntimeError("клиент сломался")


def checks_of(conn):
    return dict(conn.execute("select check_code, status from meta.check_results "
                             "where load_id = (select max(load_id) from meta.load_runs)").fetchall())


def test_version_conflict_blocks(conn, client, pauses):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    set_state(client, DAY)
    conflict = Tampered(client, lambda items: items.append({**items[0], "status": "cancelled"}))
    assert run(DAY, conn, conflict, sleep=pauses.append) == 1
    assert last_status(conn) == "blocked"
    assert checks_of(conn)["B03"] == "failed"
    assert snapshot(conn) == before
    assert one(conn, "select count(*) from staging.orders_candidate") > 0


def test_blocked_keeps_state(conn, client, pauses):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    set_state(client, DAY)
    negative = Tampered(client, lambda items: items[0].update(amount_rub=-items[0]["amount_rub"]))
    assert run(DAY, conn, negative, sleep=pauses.append) == 1
    assert last_status(conn) == "blocked"
    assert snapshot(conn) == before
    res = checks_of(conn)
    assert len(res) == 18
    assert res["B05"] == "failed" and res["B12"] == "skipped"
    assert one(conn, "select count(*) from staging.orders_candidate") > 0


def test_warnings_publish(conn, client, pauses):
    set_state(client, DAY)
    dup = Tampered(client, lambda items: items.append(dict(items[0])))
    assert run(DAY, conn, dup, sleep=pauses.append) == 0
    assert last_status(conn) == "published"
    res = checks_of(conn)
    assert [c for c, s in res.items() if s != "passed"] == ["W01"]
    assert one(conn, "select sum(orders_created) from mart.daily_revenue") == one(
        conn, "select count(*) from staging.orders") > 0


def test_null_payment_publishes_with_warning(conn, client, pauses):
    set_state(client, DAY)
    no_payment = Tampered(client, lambda items: items[0].update(payment_method=None))
    assert run(DAY, conn, no_payment, sleep=pauses.append) == 0
    assert last_status(conn) == "published"
    assert [c for c, s in checks_of(conn).items() if s != "passed"] == ["W03"]
    # окно 08-01…08-03, первая страница каждого дня
    assert one(conn, "select count(*) from staging.orders where payment_method is null") == 3


def test_unexpected_error(conn, pauses):
    assert run(DAY, conn, Broken(), sleep=pauses.append) == 3
    status, message = conn.execute("select status, message from meta.load_runs").fetchone()
    assert status == "error"
    assert "клиент сломался" in message


def test_mart_mismatch_rolls_back(conn, client, pauses, monkeypatch):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    bad = run_module.MART.replace("from staging.orders", "from staging.orders where city <> 'Москва'")
    monkeypatch.setattr(run_module, "MART", bad)
    assert load(conn, client, pauses) == 1
    assert last_status(conn) == "blocked"
    assert checks_of(conn)["B12"] == "failed"
    assert snapshot(conn) == before


def test_freshness(conn, capsys):
    assert freshness(conn, date(2026, 10, 1)) == 1
    conn.execute("""
        insert into meta.load_runs (report_date, window_from, window_to, status, finished_at) values
            ('2026-09-30', '2026-09-24', '2026-09-30', 'published', now()),
            ('2026-10-01', '2026-09-25', '2026-10-01', 'blocked', now())""")
    assert freshness(conn, date(2026, 10, 1)) == 0
    assert "2026-09-30" in capsys.readouterr().out
    assert freshness(conn, date(2026, 10, 3)) == 1


def test_freshness_stuck_running(conn, capsys):
    now = datetime(2026, 10, 1, 12, tzinfo=ZoneInfo(TZ))
    conn.execute("""
        insert into meta.load_runs (report_date, window_from, window_to, status, started_at, finished_at) values
            ('2026-09-30', '2026-09-24', '2026-09-30', 'published', %(now)s - interval '6 hours', %(now)s),
            ('2026-09-30', '2026-09-24', '2026-09-30', 'running', %(now)s - interval '61 minutes', null),
            ('2026-09-30', '2026-09-24', '2026-09-30', 'running', %(now)s - interval '59 minutes', null)""",
                 {"now": now})
    assert freshness(conn, date(2026, 10, 1), now) == 1
    out = capsys.readouterr().out
    assert "load 2 " in out and "2026-10-01 10:59:00+03:00" in out
    assert "load 3 " not in out
    assert freshness(conn, date(2026, 10, 1), now - timedelta(minutes=2)) == 0
