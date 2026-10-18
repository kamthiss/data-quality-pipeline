import argparse
import sys
from datetime import date, datetime, time, timedelta
from time import sleep as pause
from zoneinfo import ZoneInfo

import httpx

from dqpipe.config import API_URL, TIMEOUT, TZ
from dqpipe.db import apply_sql, connect
from dqpipe.run import run


def simulate(conn, client, first, last, fault="clean", fault_date=None, sleep=pause):
    """Запуски за дни first…last, каждый в 03:00 по Москве следующего дня; вернуть коды выхода."""
    codes = []
    d = first
    while d <= last:
        now = datetime.combine(d + timedelta(days=1), time(3), ZoneInfo(TZ))
        client.post("/admin/state", json={"now": now.isoformat(), "fault": fault,
                                          "fault_date": fault_date and fault_date.isoformat()}).raise_for_status()
        codes.append(run(d, conn, client, sleep))
        d += timedelta(days=1)
    return codes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    p.add_argument("--fault", default="clean")
    p.add_argument("--fault-date", type=date.fromisoformat)
    a = p.parse_args()

    with connect() as conn, httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
        apply_sql(conn)
        codes = simulate(conn, client, a.first, a.last, a.fault, a.fault_date)
    sys.exit(max(codes, default=0))


if __name__ == "__main__":
    main()
