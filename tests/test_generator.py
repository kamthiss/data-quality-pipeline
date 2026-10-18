from datetime import date

import pandas as pd
import pytest

from dqpipe.source.generator import TZ, daily_counts, generate


@pytest.fixture(scope="module")
def df():
    return generate()


@pytest.fixture(scope="module")
def v1(df):
    return df[df["version"] == 1]


def test_deterministic(df):
    pd.testing.assert_frame_equal(df, generate())


def test_counts_match_formula(v1):
    counts = daily_counts()
    actual = v1["created_at"].dt.tz_convert(TZ).dt.date.value_counts().to_dict()
    assert actual == counts


def test_expected_totals():
    counts = daily_counts()
    assert len(counts) == 61
    assert sum(counts.values()) == 84317
    assert sum(n for d, n in counts.items() if d.month == 8) == 40703
    assert sum(n for d, n in counts.items() if d.month == 9) == 43614
    assert counts[date(2026, 8, 1)] == 1380
    assert counts[date(2026, 9, 30)] == 1488
    assert min(counts.values()) == 1149
    assert max(counts.values()) == 1689
    assert sum(-(-n // 500) for n in counts.values()) == 194


def test_order_ids_follow_created_at(v1):
    assert v1["order_id"].is_unique
    assert v1["order_id"].tolist() == [f"O{k:08d}" for k in range(1, len(v1) + 1)]
    assert v1["created_at"].is_monotonic_increasing


def test_contract(df):
    for c in ("order_id", "created_at", "updated_at", "customer_id"):
        assert df[c].notna().all()
    assert set(df["status"]) == {"created", "paid", "cancelled", "refunded"}
    assert df["amount_rub"].between(0.01, 300000).all()
    assert (df["amount_rub"] <= 100000).all()
    assert (df["amount_rub"].round(2) == df["amount_rub"]).all()
    assert df["items_count"].between(1, 50).all()
    assert set(df["payment_method"]) <= {"card", "sbp", "cash_on_delivery"}
    assert (df["updated_at"] >= df["created_at"]).all()
    assert (df["available_at"] >= df["updated_at"]).all()
    assert df["customer_id"].str.fullmatch(r"C\d{6}").all()
    assert not df.duplicated(["order_id", "updated_at"]).any()


def test_refund_versions(df, v1):
    v2 = df[df["version"] == 2].set_index("order_id")
    first = v1.set_index("order_id").loc[v2.index]
    assert (v2["status"] == "refunded").all()
    assert (first["status"] == "paid").all()
    lag = v2["updated_at"] - first["updated_at"]
    assert lag.between(pd.Timedelta(days=1), pd.Timedelta(days=5)).all()
    same = ["created_at", "customer_id", "city", "amount_rub", "items_count", "payment_method"]
    pd.testing.assert_frame_equal(v2[same], first[same])
