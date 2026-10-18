import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dqpipe.config import TZ
from dqpipe.db import connect


def freshness(conn, today, now=None):
    """Напечатать последний опубликованный день и зависшие запуски; 0, если день вчерашний и зависших нет, иначе 1."""
    now = now or datetime.now(ZoneInfo(TZ))
    day, at = conn.execute(
        "select max(report_date), max(finished_at) from meta.load_runs where status = 'published'").fetchone()
    print(f"последний опубликованный день: {day or '-'}")
    print(f"последний успешный запуск: {at.astimezone(ZoneInfo(TZ)) if at else '-'}")
    stuck = conn.execute(
        "select load_id, started_at from meta.load_runs "
        "where status = 'running' and started_at < %s - interval '1 hour' order by load_id", (now,)).fetchall()
    for load_id, started in stuck:
        print(f"завис запуск: load {load_id} начат {started.astimezone(ZoneInfo(TZ))}")
    return 0 if day == today - timedelta(days=1) and not stuck else 1


def moment(s):
    # время без пояса считаем московским, а не поясом сессии Postgres
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(TZ))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=date.fromisoformat, default=datetime.now(ZoneInfo(TZ)).date())
    p.add_argument("--now", type=moment, default=datetime.now(ZoneInfo(TZ)))
    a = p.parse_args()
    with connect() as conn:
        code = freshness(conn, a.today, a.now)
    sys.exit(code)


if __name__ == "__main__":
    main()
