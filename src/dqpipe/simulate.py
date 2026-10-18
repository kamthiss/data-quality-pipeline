import argparse
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from dqpipe.config import API_URL, TIMEOUT, TZ
from dqpipe.db import apply_sql, connect
from dqpipe.run import run


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    p.add_argument("--fault", default="clean")
    p.add_argument("--fault-date", type=date.fromisoformat)
    a = p.parse_args()

    code = 0
    with connect() as conn, httpx.Client(base_url=API_URL, timeout=TIMEOUT) as client:
        apply_sql(conn)
        d = a.first
        while d <= a.last:
            now = datetime.combine(d + timedelta(days=1), time(3), ZoneInfo(TZ))
            fault_date = a.fault_date.isoformat() if a.fault_date else None
            client.post("/admin/state", json={"now": now.isoformat(), "fault": a.fault,
                                              "fault_date": fault_date}).raise_for_status()
            code = max(code, run(d, conn, client))
            d += timedelta(days=1)
    sys.exit(code)


if __name__ == "__main__":
    main()
