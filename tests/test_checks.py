from datetime import date, timedelta

from psycopg.types.json import Jsonb

from dqpipe import checks
from dqpipe.run import CANDIDATE, MART, STAGING

DAYS = [date(2026, 8, 1) + timedelta(days=i) for i in range(10)]
FIRST, LAST = DAYS[3], DAYS[-1]
CITIES = ["Москва", "Казань"]
BY_CODE = {c.code: c for c in [*checks.CHECKS, checks.B12]}


def order(day, k):
    """Строка ответа API: заказ создан днём по Москве."""
    return {"order_id": f"O{day:%m%d}{k:02d}", "created_at": f"{day}T09:{k:02d}:00Z",
            "updated_at": f"{day}T09:{k:02d}:30Z", "customer_id": f"C{k:06d}", "city": CITIES[k % 2],
            "status": "paid", "amount_rub": 1000.5 + k, "items_count": 1 + k % 3, "payment_method": "card"}


def new_load(conn, status, last):
    return conn.execute("insert into meta.load_runs (report_date, window_from, window_to, status) "
                        "values (%s, %s, %s, %s) returning load_id",
                        (last, last - timedelta(days=6), last, status)).fetchone()[0]


def put_pages(conn, load_id, items):
    for d, xs in items.items():
        conn.execute("insert into raw.api_pages (load_id, request_date, page, payload) values (%s, %s, 1, %s)",
                     (load_id, d, Jsonb({"date": str(d), "page": 1, "pages": 1, "items": xs})))


def seed(conn, change=None):
    """Опубликованы 08-01…08-09, в raw и кандидате окно 08-04…08-10 по 10 заказов в день.

    change правит строки окна после того, как контрольные итоги уже посчитаны.
    """
    items = {d: [order(d, k) for k in range(10)] for d in DAYS}
    prev = new_load(conn, "published", DAYS[-2])
    put_pages(conn, prev, {d: items[d] for d in DAYS[:-1]})
    conn.execute(CANDIDATE, (prev,))
    conn.execute(STAGING)
    conn.execute(MART)
    conn.execute("delete from staging.orders_candidate")

    load_id = new_load(conn, "running", LAST)
    window = {d: items[d] for d in DAYS[3:]}
    for d, xs in window.items():
        total = round(sum(x["amount_rub"] for x in xs), 2)
        conn.execute("insert into raw.api_summaries (load_id, request_date, payload) values (%s, %s, %s)",
                     (load_id, d, Jsonb({"date": str(d), "orders": len(xs), "amount_rub_total": total})))
    if change:
        change(window)
    put_pages(conn, load_id, window)
    conn.execute(CANDIDATE, (load_id,))
    return {"load_id": load_id, "first": FIRST, "last": LAST}


def check(conn, params, code):
    return checks.evaluate(conn, BY_CODE[code], params)


def candidate(conn, sql, *params):
    conn.execute(f"update staging.orders_candidate set {sql}", params)


def copy_row(conn, order_id, status="status"):
    conn.execute(f"""
        insert into staging.orders_candidate
        select load_id, request_date, order_id, created_at, updated_at, customer_id, city, {status},
               amount_rub, items_count, payment_method
        from staging.orders_candidate where order_id = %s""", (order_id,))


def test_clean_all_passed(conn):
    params = seed(conn)
    results = checks.run_all(conn, params)
    assert [r.check.code for r in results] == [c.code for c in checks.CHECKS]
    assert {r.check.code: r.status for r in results} == {c.code: "passed" for c in checks.CHECKS}
    conn.execute("delete from staging.orders where report_date between %s and %s", (FIRST, LAST))
    conn.execute(STAGING)
    conn.execute("delete from mart.daily_revenue")
    conn.execute(MART)
    assert check(conn, params, "B12").status == "passed"


def test_b01_missing_key_and_bad_type(conn):
    def change(w):
        w[LAST][0].pop("amount_rub")
        w[LAST][1]["items_count"] = "два"
        w[LAST][2]["city"] = None

    r = check(conn, seed(conn, change), "B01")
    assert r.status == "failed" and r.violations == 2
    assert r.sample[0]["missing"] == ["amount_rub"]
    assert r.sample[1]["missing"] == []


def test_b01_failed_skips_typed_checks(conn):
    params = seed(conn, lambda w: w[LAST][0].update(created_at="вчера"))
    status = {r.check.code: r.status for r in checks.run_all(conn, params)}
    typed = {"B02", "B03", "B04", "B05", "B06", "B07", "B08", "B11", "W02", "W03", "W05", "W06"}
    assert status["B01"] == "failed"
    assert {c for c, s in status.items() if s == "skipped"} == typed
    assert all(status[c] == "passed" for c in ("B09", "B10", "W01", "W04"))


def test_b02_empty_required(conn):
    params = seed(conn)
    candidate(conn, "customer_id = '' where order_id = 'O081001'")
    candidate(conn, "updated_at = null where order_id = 'O081002'")
    r = check(conn, params, "B02")
    assert r.status == "failed" and r.violations == 2


def test_b03_version_conflict(conn):
    params = seed(conn)
    copy_row(conn, "O081001", status="'cancelled'")
    r = check(conn, params, "B03")
    assert r.status == "failed" and r.violations == 1
    assert r.sample[0]["order_id"] == "O081001"


def test_b04_status(conn):
    params = seed(conn)
    candidate(conn, "status = 'unknown' where order_id = 'O081001'")
    r = check(conn, params, "B04")
    assert r.status == "failed" and r.sample == [{"order_id": "O081001", "status": "unknown"}]


def test_b05_amount_range(conn):
    params = seed(conn)
    candidate(conn, "amount_rub = '-5' where order_id = 'O081001'")
    candidate(conn, "amount_rub = '300000.01' where order_id = 'O081002'")
    candidate(conn, "amount_rub = '300000' where order_id = 'O081003'")
    r = check(conn, params, "B05")
    assert r.status == "failed" and r.violations == 2


def test_b06_items_count(conn):
    params = seed(conn)
    candidate(conn, "items_count = '0' where order_id = 'O081001'")
    candidate(conn, "items_count = '51' where order_id = 'O081002'")
    r = check(conn, params, "B06")
    assert r.status == "failed" and r.violations == 2


def test_b07_updated_before_created(conn):
    params = seed(conn)
    candidate(conn, "updated_at = '2026-08-10T08:00:00Z' where order_id = 'O081001'")
    r = check(conn, params, "B07")
    assert r.status == "failed" and r.violations == 1


def test_b08_summary_mismatch(conn):
    def change(w):
        w[LAST].pop()
        w[DAYS[5]][0]["amount_rub"] += 0.01

    r = check(conn, seed(conn, change), "B08")
    assert r.status == "failed"
    assert [x["request_date"] for x in r.sample] == [str(DAYS[5]), str(LAST)]
    assert r.sample[1]["api_orders"] == 10 and r.sample[1]["orders"] == 9


def test_b09_missing_day(conn):
    params = seed(conn)
    conn.execute("delete from staging.orders_candidate where request_date = %s", (DAYS[6],))
    r = check(conn, params, "B09")
    assert r.status == "failed" and r.sample == [{"day": str(DAYS[6])}]


def test_b10_report_day_missing(conn):
    params = seed(conn)
    conn.execute("delete from staging.orders_candidate where request_date = %s", (LAST,))
    r = check(conn, params, "B10")
    assert r.status == "failed" and r.sample[0]["last_day"] == str(DAYS[-2])


def test_b11_volume_drop(conn):
    params = seed(conn)
    conn.execute("delete from staging.orders_candidate where request_date = %s and order_id > 'O081003'", (LAST,))
    r = check(conn, params, "B11")
    assert r.status == "failed" and r.sample[0]["day"] == str(LAST) and r.sample[0]["orders"] == 4
    assert check(conn, params, "W05").status == "passed"


def test_w01_exact_duplicates(conn):
    params = seed(conn)
    copy_row(conn, "O081001")
    copy_row(conn, "O081002")
    r = check(conn, params, "W01")
    assert r.status == "failed" and r.violations == 2


def test_w02_empty_city(conn):
    params = seed(conn)
    candidate(conn, "city = null where order_id = 'O080501'")
    r = check(conn, params, "W02")
    assert r.status == "failed" and r.sample[0]["request_date"] == str(DAYS[4])


def test_w03_new_city_and_payment(conn):
    params = seed(conn)
    candidate(conn, "payment_method = 'crypto' where order_id = 'O081001'")
    candidate(conn, "city = 'Тверь' where order_id = 'O081002'")
    r = check(conn, params, "W03")
    assert r.status == "failed"
    assert {(x["field"], x["value"]) for x in r.sample} == {("payment_method", "crypto"), ("city", "Тверь")}


def test_w04_late_orders_for_published_day(conn):
    params = seed(conn)
    conn.execute("delete from staging.orders where order_id = 'O080501'")
    candidate(conn, "status = 'refunded', updated_at = '2026-08-07T10:00:00Z' where order_id = 'O080502'")
    r = check(conn, params, "W04")
    assert r.status == "failed"
    assert r.sample == [{"request_date": str(DAYS[4]), "order_id": "O080501"}]


def test_w05_volume_dip(conn):
    params = seed(conn)
    conn.execute("delete from staging.orders_candidate where request_date = %s and order_id > 'O081006'", (LAST,))
    r = check(conn, params, "W05")
    assert r.status == "failed" and r.sample[0]["orders"] == 7
    assert check(conn, params, "B11").status == "passed"


def test_w06_expensive_order(conn):
    params = seed(conn)
    candidate(conn, "amount_rub = '150000' where order_id = 'O081001'")
    r = check(conn, params, "W06")
    assert r.status == "failed" and r.violations == 1


def test_b12_mart_differs_from_staging(conn):
    params = seed(conn)
    assert check(conn, params, "B12").status == "passed"
    conn.execute("delete from mart.daily_revenue where report_date = %s and city = 'Казань'", (DAYS[2],))
    r = check(conn, params, "B12")
    assert r.status == "failed"
    assert r.sample[0]["mart_orders"] < r.sample[0]["staging_orders"]
