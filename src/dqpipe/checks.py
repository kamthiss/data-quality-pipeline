from dataclasses import dataclass

from psycopg.types.json import Jsonb

from dqpipe.config import TZ

# distinct схлопывает точные дубли
TYPED = f"""
with typed as (
    select distinct request_date, order_id, created_at::timestamptz as created_at,
           updated_at::timestamptz as updated_at, customer_id, city, status,
           amount_rub::numeric(12, 2) as amount_rub, items_count::integer as items_count,
           payment_method, (created_at::timestamptz at time zone '{TZ}')::date as report_date
    from staging.orders_candidate
)
"""

LATEST = TYPED + """,
latest as (
    select distinct on (order_id) * from typed
    order by order_id, updated_at desc
)
"""

# состояние после принятия: дни окна из кандидата, остальные из staging
AFTER = """
with after as (
    select request_date as day, order_id from staging.orders_candidate
    union
    select report_date, order_id from staging.orders
    where report_date not between %(first)s and %(last)s
)
"""

VOLUME = AFTER + """,
daily as (
    select day, count(distinct order_id) as orders from after group by day
),
volume as (
    select w.day, coalesce(d.orders, 0) as orders,
           (select count(*) from daily p where p.day between w.day - 7 and w.day - 1) as prev_days,
           (select percentile_cont(0.5) within group (order by p.orders)
            from daily p where p.day between w.day - 7 and w.day - 1) as median
    from (select g::date as day from generate_series(%(first)s::date, %(last)s::date, interval '1 day') g) w
    left join daily d using (day)
)
"""


@dataclass(frozen=True)
class Check:
    code: str
    level: str
    text: str
    sql: str
    typed: bool = False


@dataclass
class Result:
    check: Check
    status: str
    violations: int | None = None
    sample: list | None = None
    details: str | None = None


CHECKS = [
    Check("B01", "error", "все поля на месте и приводятся к своим типам", """
        with items as (
            select p.request_date, p.page, i
            from raw.api_pages p, jsonb_array_elements(p.payload->'items') i
            where p.load_id = %(load_id)s
        )
        select request_date, page, i->>'order_id' as order_id,
               array(select k from unnest(array['order_id', 'created_at', 'updated_at', 'customer_id', 'city',
                                                'status', 'amount_rub', 'items_count', 'payment_method']) k
                     where not i ? k) as missing,
               i as item
        from items
        where not i ?& array['order_id', 'created_at', 'updated_at', 'customer_id', 'city',
                             'status', 'amount_rub', 'items_count', 'payment_method']
           or i->>'created_at' is not null and not pg_input_is_valid(i->>'created_at', 'timestamptz')
           or i->>'updated_at' is not null and not pg_input_is_valid(i->>'updated_at', 'timestamptz')
           or i->>'amount_rub' is not null and not pg_input_is_valid(i->>'amount_rub', 'numeric(12, 2)')
           or i->>'items_count' is not null and not pg_input_is_valid(i->>'items_count', 'integer')
    """),
    Check("B02", "error", "order_id, created_at, updated_at, customer_id не пустые", TYPED + """
        select order_id, created_at, updated_at, customer_id from typed
        where coalesce(order_id, '') = '' or created_at is null or updated_at is null
           or coalesce(customer_id, '') = ''
    """, typed=True),
    Check("B03", "error", "после удаления дублей order_id уникален, конфликтов версий нет", TYPED + """
        select order_id, updated_at, count(*) as rows from typed
        group by order_id, updated_at
        having count(*) > 1
        order by order_id
    """, typed=True),
    Check("B04", "error", "status из допустимого списка", TYPED + """
        select order_id, status from typed
        where status is null or status not in ('created', 'paid', 'cancelled', 'refunded')
    """, typed=True),
    Check("B05", "error", "amount_rub больше 0 и не больше 300 000", TYPED + """
        select order_id, amount_rub from typed
        where amount_rub is null or amount_rub <= 0 or amount_rub > 300000
    """, typed=True),
    Check("B06", "error", "items_count от 1 до 50", TYPED + """
        select order_id, items_count from typed
        where items_count is null or items_count not between 1 and 50
    """, typed=True),
    Check("B07", "error", "updated_at не раньше created_at", TYPED + """
        select order_id, created_at, updated_at from typed
        where updated_at < created_at
    """, typed=True),
    Check("B08", "error", "число заказов и сумма совпадают с контрольным итогом API", LATEST + """,
        got as (
            select request_date, count(*) as orders, sum(amount_rub) as amount
            from latest group by request_date
        )
        select s.request_date,
               (s.payload->>'orders')::int as api_orders, coalesce(g.orders, 0) as orders,
               (s.payload->>'amount_rub_total')::numeric as api_amount, coalesce(g.amount, 0) as amount
        from raw.api_summaries s
        left join got g using (request_date)
        where s.load_id = %(load_id)s
          and ((s.payload->>'orders')::int <> coalesce(g.orders, 0)
               or round((s.payload->>'amount_rub_total')::numeric, 2) <> coalesce(g.amount, 0))
        order by s.request_date
    """, typed=True),
    Check("B09", "error", "в загруженном периоде нет пропущенных дней", AFTER + """
        select g::date as day
        from generate_series((select min(day) from after), %(last)s::date, interval '1 day') g
        where g::date not in (select day from after)
    """),
    Check("B10", "error", "последний опубликованный день — вчерашний", AFTER + """
        select max(day) as last_day, %(last)s::date as report_date from after
        having max(day) is distinct from %(last)s::date
    """),
    Check("B11", "error", "заказов за день не меньше 50% медианы предыдущих 7 дней", VOLUME + """
        select * from volume
        where prev_days >= 3 and orders < 0.5 * median
    """, typed=True),
    Check("W01", "warn", "удалены точные дубли", """
        select order_id, updated_at, status, copy from (
            select *, row_number() over (
                partition by request_date, order_id, created_at, updated_at, customer_id, city,
                             status, amount_rub, items_count, payment_method) as copy
            from staging.orders_candidate
        ) c
        where copy > 1
    """),
    Check("W02", "warn", "пустой город больше чем у 1% заказов за день", """
        select request_date, count(distinct order_id) as orders,
               count(distinct order_id) filter (where coalesce(city, '') = '') as empty_city
        from staging.orders_candidate
        group by request_date
        having count(distinct order_id) filter (where coalesce(city, '') = '')
               > 0.01 * count(distinct order_id)
    """, typed=True),
    Check("W03", "warn", "появился новый город или способ оплаты", """
        select 'payment_method' as field, payment_method as value, count(*) as rows
        from staging.orders_candidate
        where payment_method not in ('card', 'sbp', 'cash_on_delivery')
        group by payment_method
        union all
        select 'city', city, count(*)
        from staging.orders_candidate
        where city <> ''
          and exists (select 1 from staging.orders where report_date < %(first)s)
          and city not in (select city from staging.orders
                           where report_date < %(first)s and city is not null)
        group by city
    """, typed=True),
    Check("W04", "warn", "пришли заказы за уже опубликованные дни, эти дни пересобраны", """
        select distinct request_date, order_id from staging.orders_candidate c
        where request_date <= (select max(report_date) from meta.load_runs where status = 'published')
          and not exists (select 1 from staging.orders o where o.order_id = c.order_id)
        order by request_date, order_id
    """),
    Check("W05", "warn", "заказов за день от 50% до 80% медианы предыдущих 7 дней", VOLUME + """
        select * from volume
        where prev_days >= 3 and orders >= 0.5 * median and orders < 0.8 * median
    """, typed=True),
    Check("W06", "warn", "есть заказы дороже 100 000 ₽", LATEST + """
        select order_id, amount_rub from latest
        where amount_rub > 100000
    """, typed=True),
]

# выполняется в транзакции публикации, после пересборки витрины
B12 = Check("B12", "error", "итоги витрины совпадают с итогами staging", """
    with m as (
        select coalesce(sum(orders_created), 0) as orders, coalesce(sum(revenue_rub), 0) as revenue
        from mart.daily_revenue
    ),
    s as (
        select count(*) as orders, coalesce(sum(amount_rub) filter (where status = 'paid'), 0) as revenue
        from staging.orders
    )
    select m.orders as mart_orders, s.orders as staging_orders,
           m.revenue as mart_revenue, s.revenue as staging_revenue
    from m, s
    where m.orders <> s.orders or m.revenue <> s.revenue
""")


def evaluate(conn, check, params):
    """Выполнить проверку: число нарушений и до 20 примеров."""
    n, sample = conn.execute(f"""
        with v as ({check.sql})
        select count(*), (select jsonb_agg(s) from (select * from v limit 20) s) from v
    """, params).fetchone()
    return Result(check, "failed" if n else "passed", n, sample)


def run_all(conn, params):
    """Проверки кандидата до публикации, без B12."""
    results = [evaluate(conn, CHECKS[0], params)]
    for c in CHECKS[1:]:
        if c.typed and results[0].status == "failed":
            results.append(Result(c, "skipped", details="B01 не прошла: поля не приводятся к типам"))
        else:
            results.append(evaluate(conn, c, params))
    return results


def save(conn, load_id, results):
    """Записать результаты в meta.check_results."""
    with conn.cursor() as cur:
        cur.executemany(
            "insert into meta.check_results (load_id, check_code, level, status, violations, sample, details) "
            "values (%s, %s, %s, %s, %s, %s, %s)",
            [(load_id, r.check.code, r.check.level, r.status, r.violations,
              Jsonb(r.sample) if r.sample else None, r.details) for r in results])
