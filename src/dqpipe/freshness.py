import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dqpipe.config import TZ
from dqpipe.db import connect


def freshness(conn, today):
    """Напечатать последний опубликованный день; 0, если это вчера относительно today, иначе 1."""
    day, at = conn.execute(
        "select max(report_date), max(finished_at) from meta.load_runs where status = 'published'").fetchone()
    print(f"последний опубликованный день: {day or '-'}")
    print(f"последний успешный запуск: {at.astimezone(ZoneInfo(TZ)) if at else '-'}")
    return 0 if day == today - timedelta(days=1) else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=date.fromisoformat, default=datetime.now(ZoneInfo(TZ)).date())
    a = p.parse_args()
    with connect() as conn:
        code = freshness(conn, a.today)
    sys.exit(code)


if __name__ == "__main__":
    main()
