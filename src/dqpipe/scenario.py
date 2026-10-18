import argparse
import contextlib
import io
import socket
import subprocess
import sys
import time
from datetime import date, timedelta
from decimal import Decimal

import httpx
from psycopg import sql
from psycopg.conninfo import make_conninfo

from dqpipe.config import API_URL, DB_URL, START, TIMEOUT
from dqpipe.db import apply_sql, connect
from dqpipe.freshness import freshness
from dqpipe.simulate import simulate

DAY = date(2026, 9, 30)
LATE = date(2026, 9, 28)
TODAY = date(2026, 10, 1)
REVENUE = Decimal("226843468.87")
PREFIX = "dq_scn"
PORT = httpx.URL(API_URL).port

PUBLISHED = {"код": 0, "статус": "published", "не прошли": "—", "предупреждения": "—", "пропущены": "—",
             "повторы": "—", "витрина": "эталон", "свежесть": 0, "восстановление": f"эталон {REVENUE}"}
BLOCKED = {**PUBLISHED, "код": 1, "статус": "blocked", "пропущены": "B12", "витрина": "как 09-29", "свежесть": 1}
EXPECTED = {
    "clean": PUBLISHED,
    "rerun": PUBLISHED,
    "duplicates": {**PUBLISHED, "предупреждения": "W01 22"},
    "api_500": {**PUBLISHED, "повторы": "2 (1, 2 с)"},
    "api_down": {**BLOCKED, "код": 2, "статус": "failed_source", "не прошли": "не запускались",
                 "пропущены": "—", "повторы": "3 (1, 2, 4 с)"},
    "partial_page": {**BLOCKED, "не прошли": "B08 988/1488", "предупреждения": "W05 988/1459"},
    "missing_day": {**BLOCKED, "не прошли": "B09 1, B10 1, B11 0/1459"},
    "schema_drift": {**BLOCKED, "не прошли": "B01 1488",
                     "пропущены": "B02 B03 B04 B05 B06 B07 B08 B11 B12 W02 W03 W05 W06"},
    "bad_values": {**BLOCKED, "не прошли": "B04 3, B05 5"},
    "late_data": {**PUBLISHED, "предупреждения": "W04 40", "витрина": "09-28: −40 → эталон"},
    "volume_drop": {**PUBLISHED, "предупреждения": "W05 893/1459", "витрина": "09-30: 893"},
}


def recreate(admin, name, template="template1"):
    admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
    admin.execute(sql.SQL("create database {} template {}").format(sql.Identifier(name), sql.Identifier(template)))
    return make_conninfo(DB_URL, dbname=name)


def mart(conn):
    return conn.execute("select md5(string_agg(t::text, '|' order by report_date, city)), sum(revenue_rub) "
                        "from mart.daily_revenue t").fetchone()


def orders(conn, day):
    return conn.execute("select sum(orders_created) from mart.daily_revenue where report_date = %s",
                        (day,)).fetchone()[0]


def label(code, violations, s):
    if code == "B08":
        return f"B08 {s['orders']}/{s['api_orders']}"
    if code in ("B11", "W05"):
        return f"{code} {s['orders']}/{s['median']:g}"
    return f"{code} {violations}"


def prepare(admin, client, prefix=PREFIX):
    """Чистые прогоны с 08-01: шаблоны {prefix}_0927, {prefix}_0929 и эталон {prefix}_ref; вернуть их хэши."""
    with connect(recreate(admin, f"{prefix}_0927")) as conn:
        apply_sql(conn)
        simulate(conn, client, START, LATE - timedelta(days=1))
    with connect(recreate(admin, f"{prefix}_0929", f"{prefix}_0927")) as conn:
        simulate(conn, client, LATE, DAY - timedelta(days=1))
        base = mart(conn)[0]
    with connect(recreate(admin, f"{prefix}_ref", f"{prefix}_0929")) as conn:
        simulate(conn, client, DAY, DAY)
        ref = mart(conn)[0]
        late = orders(conn, LATE)
    return {"base": base, "ref": ref, "late": late}


def play(name, admin, client, refs, prefix=PREFIX, sleep=time.sleep):
    """Сбойный запуск за DAY на копии шаблона и восстановление; вернуть факты в форме EXPECTED."""
    fault = "clean" if name == "rerun" else name
    pauses = []

    def wait(s):
        pauses.append(s)
        sleep(s)

    base = f"{prefix}_0927" if name == "late_data" else f"{prefix}_0929"
    with connect(recreate(admin, f"{prefix}_work", base)) as conn:
        if name == "late_data":
            simulate(conn, client, LATE, LATE, fault, DAY)
            lag = refs["late"] - orders(conn, LATE)
            simulate(conn, client, LATE + timedelta(days=1), DAY - timedelta(days=1), fault, DAY)
        if name == "rerun":
            simulate(conn, client, DAY, DAY)
            first = mart(conn)[0]

        [code] = simulate(conn, client, DAY, DAY, fault, DAY, wait)
        load_id, status = conn.execute(
            "select load_id, status from meta.load_runs order by load_id desc limit 1").fetchone()
        res = conn.execute("select check_code, level, status, violations, sample->0 from meta.check_results "
                           "where load_id = %s order by check_code", (load_id,)).fetchall()
        h = mart(conn)[0]
        if h == refs["base"]:
            shown = "как 09-29"
        elif h == refs["ref"]:
            shown = "эталон"
        else:
            shown = f"09-30: {orders(conn, DAY)}"
        if name == "late_data":
            shown = f"09-28: −{lag} → {shown}"
        if name == "rerun" and first != h:
            shown = "изменилась при повторе"
        fresh = freshness(conn, TODAY)

        simulate(conn, client, DAY, DAY)
        h, revenue = mart(conn)

    failed = [label(c, v, s) for c, lvl, st, v, s in res if st == "failed" and lvl == "error"]
    warned = [label(c, v, s) for c, lvl, st, v, s in res if st == "failed" and lvl == "warn"]
    return {
        "код": code,
        "статус": status,
        "не прошли": ", ".join(failed) or ("—" if res else "не запускались"),
        "предупреждения": ", ".join(warned) or "—",
        "пропущены": " ".join(c for c, _, st, _, _ in res if st == "skipped") or "—",
        "повторы": f"{len(pauses)} ({', '.join(map(str, pauses))} с)" if pauses else "—",
        "витрина": shown,
        "свежесть": fresh,
        "восстановление": f"{'эталон' if h == refs['ref'] else 'не эталон'} {revenue}",
    }


def port_busy():
    with socket.socket() as s:
        return s.connect_ex(("localhost", PORT)) == 0


def healthy(client):
    try:
        return client.get("/health").is_success
    except httpx.TransportError:
        return False


def wait_for(cond, what, timeout=60):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise RuntimeError(f"не дождался за {timeout} с: {what}")
        time.sleep(0.2)


@contextlib.contextmanager
def api():
    """Поднять API через uvicorn и погасить; порт должен быть свободен до старта и после остановки."""
    # после make demo uvicorn ещё держит порт, пока завершается
    wait_for(lambda: not port_busy(), f"порт {PORT} свободен")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dqpipe.source.api:app",
                             "--port", str(PORT), "--log-level", "warning"])
    try:
        with httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
            wait_for(lambda: healthy(client), "API отвечает на /health")
            yield client
    finally:
        proc.terminate()
        proc.wait()
        wait_for(lambda: not port_busy(), f"порт {PORT} освободился")


def table(facts):
    cols = list(PUBLISHED)
    rows = [["сценарий", "", *cols, "итог"]]
    for name, fact in facts.items():
        diff = [c for c in cols if fact[c] != EXPECTED[name][c]]
        rows.append([name, "ожид.", *(str(EXPECTED[name][c]) for c in cols), ""])
        rows.append(["", "факт", *(str(fact[c]) for c in cols), "≠ " + ", ".join(diff) if diff else "ок"])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(x.ljust(w) for x, w in zip(r, widths)).rstrip())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("names", nargs="*", choices=list(EXPECTED))
    a = p.parse_args()
    t = time.monotonic()
    facts = {}
    with connect() as admin, api() as client:
        with contextlib.redirect_stdout(io.StringIO()):
            refs = prepare(admin, client)
        print(f"шаблоны собраны за {time.monotonic() - t:.0f} с")
        for name in a.names or EXPECTED:
            print(f"\n== {name}")
            facts[name] = play(name, admin, client, refs)
    print()
    table(facts)
    print(f"\nвсего {time.monotonic() - t:.0f} с")
    sys.exit(0 if all(facts[n] == EXPECTED[n] for n in facts) else 1)


if __name__ == "__main__":
    main()
