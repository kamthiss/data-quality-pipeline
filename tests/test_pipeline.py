from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from dqpipe.config import DB_URL, TZ
from dqpipe.db import connect, reset
from dqpipe.run import run
from dqpipe.source.api import app
from dqpipe.source.generator import OUT

DAY = date(2026, 8, 3)


@pytest.fixture(scope="module")
def admin():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("drop database if exists dq_test with (force)")
        c.execute("create database dq_test")
        yield c
        c.execute("drop database dq_test with (force)")


@pytest.fixture
def conn(admin):
    with connect(make_conninfo(DB_URL, dbname="dq_test")) as c:
        reset(c)
        yield c


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


class Conflicting:
    """Добавляет в первую страницу вторую строку заказа с тем же updated_at."""

    def __init__(self, client):
        self.client = client

    def get(self, path, params):
        r = self.client.get(path, params=params)
        if path != "/orders" or params["page"] != 1:
            return r
        body = r.json()
        body["items"].append({**body["items"][0], "status": "cancelled"})
        return httpx.Response(200, json=body)


def test_version_conflict_blocks(conn, client, pauses):
    assert load(conn, client, pauses) == 0
    before = snapshot(conn)
    set_state(client, DAY)
    assert run(DAY, conn, Conflicting(client), sleep=pauses.append) == 1
    assert last_status(conn) == "blocked"
    assert snapshot(conn) == before
    assert one(conn, "select count(*) from staging.orders_candidate") > 0
