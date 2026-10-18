import pytest
from fastapi.testclient import TestClient

from dqpipe import scenario
from dqpipe.source.api import app

PREFIX = "dq_test_scn"

pytestmark = pytest.mark.scenario


@pytest.fixture(scope="module")
def bench(admin):
    with TestClient(app) as client:
        yield client, scenario.prepare(admin, client, PREFIX)
    for db in ("0927", "0929", "ref", "work"):
        admin.execute(f"drop database if exists {PREFIX}_{db} with (force)")


@pytest.mark.parametrize("name", list(scenario.EXPECTED))
def test_scenario(admin, bench, name):
    client, refs = bench
    assert scenario.play(name, admin, client, refs, PREFIX, sleep=lambda s: None) == scenario.EXPECTED[name]
