from collections import Counter
from datetime import timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dqpipe.source.api import FIELDS, app
from dqpipe.source.generator import OUT, TZ, daily_counts

NOW = "2026-10-01T03:00:00+03:00"
DAY = "2026-09-30"
OTHER = "2026-09-29"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def set_state(client, fault="clean", fault_date=DAY, now=NOW):
    r = client.post("/admin/state", json={"now": now, "fault": fault, "fault_date": fault_date})
    assert r.status_code == 200


def fetch(client, day):
    first = client.get("/orders", params={"date": day, "page": 1}).json()
    pages = [first["items"]]
    for p in range(2, first["pages"] + 1):
        r = client.get("/orders", params={"date": day, "page": p})
        assert r.status_code == 200
        pages.append(r.json()["items"])
    return first["pages"], pages


def summary(client, day):
    r = client.get("/orders/summary", params={"date": day})
    assert r.status_code == 200
    return r.json()


def rows(pages):
    return [x for p in pages for x in p]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_clean(client):
    set_state(client)
    pages, items = fetch(client, DAY)
    assert pages == 3
    assert [len(p) for p in items] == [500, 500, 488]
    flat = rows(items)
    assert set(flat[0]) == set(FIELDS)
    assert len({x["order_id"] for x in flat}) == 1488
    s = summary(client, DAY)
    assert s["orders"] == 1488
    assert s["amount_rub_total"] == pytest.approx(sum(x["amount_rub"] for x in flat), abs=0.01)
    ids = [x["order_id"] for x in flat]
    assert ids == sorted(ids)


def test_pagination_all_days(client):
    set_state(client)
    total_pages = 0
    for d, n in daily_counts().items():
        pages, items = fetch(client, d.isoformat())
        flat = rows(items)
        total_pages += pages
        assert len(flat) == len({x["order_id"] for x in flat}) == n
        assert summary(client, d.isoformat())["orders"] == n
    assert total_pages == 194


def test_out_of_range_page(client):
    set_state(client)
    r = client.get("/orders", params={"date": DAY, "page": 5}).json()
    assert r["pages"] == 3 and r["items"] == []
    assert client.get("/orders", params={"date": DAY, "page": 0}).status_code == 422


def test_empty_day(client):
    set_state(client)
    r = client.get("/orders", params={"date": "2026-10-05", "page": 1}).json()
    assert r["pages"] == 0 and r["items"] == []
    assert summary(client, "2026-10-05") == {"date": "2026-10-05", "orders": 0, "amount_rub_total": 0}


def test_latest_visible_version(client):
    df = pd.read_parquet(OUT)
    v2 = df[df["version"] == 2].iloc[0]
    day = v2["created_at"].tz_convert(TZ).date().isoformat()

    def status_at(now):
        set_state(client, now=now.isoformat())
        return next(x["status"] for x in rows(fetch(client, day)[1]) if x["order_id"] == v2["order_id"])

    assert status_at(v2["available_at"] - timedelta(seconds=1)) == "paid"
    assert status_at(v2["available_at"]) == "refunded"


def test_duplicates(client):
    set_state(client)
    clean = summary(client, DAY)
    set_state(client, "duplicates")
    pages, items = fetch(client, DAY)
    flat = rows(items)
    assert pages == 4
    assert [len(p) for p in items] == [500, 500, 500, 10]
    assert len(flat) == 1510
    assert len({x["order_id"] for x in flat}) == 1488
    counts = Counter(x["order_id"] for x in flat)
    dups = [i for i, k in counts.items() if k == 2]
    assert len(dups) == 22 and max(counts.values()) == 2
    for i in dups:
        a, b = [x for x in flat if x["order_id"] == i]
        assert a == b
    assert summary(client, DAY) == clean
    assert fetch(client, OTHER)[0] == 3


def test_api_500(client):
    set_state(client, "api_500")
    for _ in range(2):
        assert client.get("/orders", params={"date": DAY, "page": 1}).status_code == 500
    assert client.get("/orders", params={"date": OTHER, "page": 1}).status_code == 200
    assert client.get("/orders", params={"date": DAY, "page": 1}).status_code == 200
    assert summary(client, DAY)["orders"] == 1488
    set_state(client, "api_500")
    assert client.get("/orders", params={"date": DAY, "page": 1}).status_code == 500


def test_api_down(client):
    set_state(client, "api_down")
    for _ in range(3):
        assert client.get("/orders", params={"date": DAY, "page": 1}).status_code == 503
        assert client.get("/orders/summary", params={"date": DAY}).status_code == 503
    assert client.get("/orders", params={"date": OTHER, "page": 1}).status_code == 200


def test_partial_page(client):
    set_state(client, "partial_page")
    pages, items = fetch(client, DAY)
    assert pages == 3
    assert [len(p) for p in items] == [500, 0, 488]
    assert len(rows(items)) == 988
    assert summary(client, DAY)["orders"] == 1488


def test_missing_day(client):
    set_state(client, "missing_day")
    r = client.get("/orders", params={"date": DAY, "page": 1}).json()
    assert r["pages"] == 0 and r["items"] == []
    assert summary(client, DAY) == {"date": DAY, "orders": 0, "amount_rub_total": 0}
    assert summary(client, OTHER)["orders"] > 0


def test_schema_drift(client):
    set_state(client, "schema_drift")
    flat = rows(fetch(client, DAY)[1])
    assert len(flat) == 1488
    assert all("amount" in x and "amount_rub" not in x for x in flat)
    assert summary(client, DAY)["orders"] == 1488
    assert "amount_rub" in fetch(client, OTHER)[1][0][0]


def test_bad_values(client):
    set_state(client)
    clean = {x["order_id"]: x for x in rows(fetch(client, DAY)[1])}
    set_state(client, "bad_values")
    flat = rows(fetch(client, DAY)[1])
    neg = [x for x in flat if x["amount_rub"] < 0]
    unknown = [x for x in flat if x["status"] == "unknown"]
    assert len(neg) == 5 and len(unknown) == 3
    assert not {x["order_id"] for x in neg} & {x["order_id"] for x in unknown}
    assert all(x["amount_rub"] == -clean[x["order_id"]]["amount_rub"] for x in neg)
    s = summary(client, DAY)
    assert s["orders"] == 1488
    assert s["amount_rub_total"] == pytest.approx(sum(x["amount_rub"] for x in flat), abs=0.01)


def test_late_data(client):
    late = "2026-09-28"
    n = daily_counts()[pd.Timestamp(late).date()]
    set_state(client, "late_data", now="2026-09-30T09:00:00+03:00")
    flat = rows(fetch(client, late)[1])
    assert len(flat) == n - 40
    s = summary(client, late)
    assert s["orders"] == n - 40
    assert s["amount_rub_total"] == pytest.approx(sum(x["amount_rub"] for x in flat), abs=0.01)
    assert summary(client, OTHER)["orders"] == daily_counts()[pd.Timestamp(OTHER).date()]

    set_state(client, "late_data", now="2026-09-30T11:59:59+03:00")
    assert summary(client, late)["orders"] == n - 40
    set_state(client, "late_data", now="2026-09-30T12:00:00+03:00")
    assert len(rows(fetch(client, late)[1])) == n
    assert summary(client, late)["orders"] == n
    set_state(client, "late_data")
    assert summary(client, late)["orders"] == n


def test_volume_drop(client):
    set_state(client)
    clean = {x["order_id"]: x for x in rows(fetch(client, DAY)[1])}
    set_state(client, "volume_drop")
    pages, items = fetch(client, DAY)
    flat = rows(items)
    assert pages == 2
    assert [len(p) for p in items] == [500, 393]
    assert len({x["order_id"] for x in flat}) == 893 == 1488 - 595
    assert all(x == clean[x["order_id"]] for x in flat)
    s = summary(client, DAY)
    assert s["orders"] == 893
    assert s["amount_rub_total"] == pytest.approx(sum(x["amount_rub"] for x in flat), abs=0.01)
    assert [x["order_id"] for x in rows(fetch(client, DAY)[1])] == [x["order_id"] for x in flat]
    assert summary(client, OTHER)["orders"] == daily_counts()[pd.Timestamp(OTHER).date()]


def test_admin_state_validation(client):
    r = client.post("/admin/state", json={"now": NOW, "fault": "duplicates", "fault_date": None})
    assert r.status_code == 422
    assert "fault_date обязателен" in r.text
    r = client.post("/admin/state", json={"now": NOW, "fault": "meteor", "fault_date": DAY})
    assert r.status_code == 422
    assert "duplicates" in r.json()["detail"][0]["msg"]
    set_state(client, fault_date=None)
