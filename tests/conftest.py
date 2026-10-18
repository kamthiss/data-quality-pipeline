import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from dqpipe.config import DB_URL
from dqpipe.db import connect, reset


@pytest.fixture(scope="session")
def admin():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("drop database if exists dq_test with (force)")
        c.execute("create database dq_test")
        yield c
        c.execute("drop database dq_test with (force)")


@pytest.fixture
def conn(admin):
    with connect(make_conninfo(DB_URL, dbname="dq_test")) as c:
        reset(c)
        yield c
