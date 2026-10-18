import argparse
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from psycopg.types.json import Jsonb

from dqpipe.config import API_URL, PAUSES, START, TIMEOUT, TZ, WINDOW_DAYS
from dqpipe.db import apply_sql, connect

FIELDS = ["order_id", "created_at", "updated_at", "customer_id", "city", "status",
          "amount_rub", "items_count", "payment_method"]

CANDIDATE = f"""
insert into staging.orders_candidate (load_id, request_date, {", ".join(FIELDS)})
select p.load_id, p.request_date, {", ".join(f"i->>'{f}'" for f in FIELDS)}
from raw.api_pages p, jsonb_array_elements(p.payload->'items') i
where p.load_id = %s
"""

# distinct схлопывает точные дубли
TYPED = """
with typed as (
    select distinct order_id, created_at::timestamptz as created_at,
           updated_at::timestamptz as updated_at, customer_id, city, status,
           amount_rub::numeric(12, 2) as amount_rub, items_count::integer as items_count,
           payment_method
    from staging.orders_candidate
)
"""

CONFLICTS = TYPED + """
select order_id from typed
group by order_id, updated_at
having count(*) > 1
order by order_id
"""

STAGING = TYPED + f"""
insert into staging.orders ({", ".join(FIELDS)}, report_date)
select distinct on (order_id) {", ".join(FIELDS)}, (created_at at time zone '{TZ}')::date
from typed
order by order_id, updated_at desc
"""

MART = """
insert into mart.daily_revenue
select report_date,
       coalesce(nullif(city, ''), 'не указан'),
       count(*),
       count(*) filter (where status = 'paid'),
       coalesce(sum(amount_rub) filter (where status = 'paid'), 0),
       round(count(*) filter (where status = 'cancelled')::numeric / count(*), 4)
from staging.orders
group by 1, 2
"""


class SourceError(Exception):
    """Источник не ответил после всех повторов."""


def get(client, path, params, sleep):
    """GET с повторами на 5xx и сетевых ошибках."""
    for pause in (0, *PAUSES):
        if pause:
            sleep(pause)
        try:
            r = client.get(path, params=params)
        except httpx.TransportError as e:
            err = repr(e)
            continue
        if r.status_code >= 500:
            err = f"HTTP {r.status_code}"
            continue
        if r.is_error:
            raise SourceError(f"{path} {params}: HTTP {r.status_code}")
        return r.json()
    raise SourceError(f"{path} {params}: {err} после {len(PAUSES)} повторов")


def window(day):
    """Дни загрузки: до WINDOW_DAYS дней по day включительно, не раньше START."""
    first = max(START, day - timedelta(days=WINDOW_DAYS - 1))
    return [first + timedelta(days=i) for i in range((day - first).days + 1)]


def extract(conn, client, load_id, days, sleep):
    for d in days:
        params = {"date": d.isoformat()}
        s = get(client, "/orders/summary", params, sleep)
        conn.execute("insert into raw.api_summaries (load_id, request_date, payload) values (%s, %s, %s)",
                     (load_id, d, Jsonb(s)))
        page = 1
        while True:
            r = get(client, "/orders", {**params, "page": page}, sleep)
            conn.execute("insert into raw.api_pages (load_id, request_date, page, payload) values (%s, %s, %s, %s)",
                         (load_id, d, page, Jsonb(r)))
            if page >= r["pages"]:
                break
            page += 1


def finish(conn, load_id, day, status, message=None):
    conn.execute("""
        update meta.load_runs
        set status = %s, message = %s, finished_at = clock_timestamp(),
            rows_fetched = (select coalesce(sum(jsonb_array_length(payload->'items')), 0)
                            from raw.api_pages where load_id = %s)
        where load_id = %s""", (status, message, load_id, load_id))
    print(f"{day} load {load_id}: {status}" + (f" — {message}" if message else ""))


def run(day, conn, client, sleep=time.sleep):
    """Загрузить окно до day и опубликовать staging и витрину; вернуть код выхода."""
    days = window(day)
    load_id = conn.execute(
        "insert into meta.load_runs (report_date, window_from, window_to, status) "
        "values (%s, %s, %s, 'running') returning load_id",
        (day, days[0], days[-1])).fetchone()[0]

    try:
        extract(conn, client, load_id, days, sleep)
    except SourceError as e:
        finish(conn, load_id, day, "failed_source", str(e))
        return 2

    # отдельная транзакция: при остановке кандидат остаётся для разбора
    with conn.transaction():
        conn.execute("delete from staging.orders_candidate")
        conn.execute(CANDIDATE, (load_id,))

    # здесь будут блокирующие проверки B01–B12
    bad = [r[0] for r in conn.execute(CONFLICTS)]
    if bad:
        finish(conn, load_id, day, "blocked",
               f"конфликт версий: у {len(bad)} order_id разные строки с одинаковым updated_at, "
               f"например {', '.join(bad[:5])}")
        return 1

    with conn.transaction():
        conn.execute("delete from staging.orders where report_date between %s and %s", (days[0], days[-1]))
        conn.execute(STAGING)
        # delete, а не truncate: читатели видят старую витрину до коммита, без блокировки
        conn.execute("delete from mart.daily_revenue")
        conn.execute(MART)
        finish(conn, load_id, day, "published")
    return 0


def main():
    yesterday = datetime.now(ZoneInfo(TZ)).date() - timedelta(days=1)
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=date.fromisoformat, default=yesterday)
    a = p.parse_args()
    with connect() as conn, httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
        apply_sql(conn)
        code = run(a.date, conn, client)
    sys.exit(code)


if __name__ == "__main__":
    main()
