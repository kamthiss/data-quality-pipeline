import argparse
from pathlib import Path

import psycopg

from dqpipe.config import DB_URL

SQL = Path(__file__).resolve().parents[2] / "sql"
SCHEMAS = ["mart", "staging", "raw", "meta"]


def connect(url=DB_URL):
    """Соединение в autocommit; транзакции открываются явно."""
    return psycopg.connect(url, autocommit=True)


def apply_sql(conn):
    """Применить sql/*.sql по порядку, идемпотентно."""
    for f in sorted(SQL.glob("*.sql")):
        conn.execute(f.read_text())


def reset(conn):
    """Снести схемы пайплайна и создать заново."""
    for s in SCHEMAS:
        conn.execute(f"drop schema if exists {s} cascade")
    apply_sql(conn)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()
    with connect() as conn:
        if a.reset:
            reset(conn)
        else:
            apply_sql(conn)


if __name__ == "__main__":
    main()
