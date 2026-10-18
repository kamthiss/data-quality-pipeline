import math
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

START = date(2026, 8, 1)
DAYS = 61
TZ = "Europe/Moscow"
ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get("DQ_SOURCE_PATH", ROOT / "data/source/orders.parquet"))

WEEKDAY_W = [0.95, 0.95, 1.00, 1.00, 1.05, 1.15, 1.10]
CITIES = {
    "Москва": 0.35,
    "Санкт-Петербург": 0.20,
    "Казань": 0.10,
    "Екатеринбург": 0.10,
    "Новосибирск": 0.10,
    "Нижний Новгород": 0.08,
    "Краснодар": 0.07,
}
PAYMENTS = {"card": 0.65, "sbp": 0.30, "cash_on_delivery": 0.05}
STATUSES = {"paid": 0.83, "cancelled": 0.12, "created": 0.05}


def daily_counts():
    """Число заказов по отчётным дням по формуле."""
    out = {}
    for i in range(DAYS):
        d = START + timedelta(days=i)
        out[d] = math.floor(1200 * WEEKDAY_W[d.weekday()] * (1 + 0.004 * i) + 0.5)
    return out


def seconds(rng, lo, hi, n):
    return pd.to_timedelta(rng.uniform(lo, hi, n), unit="s")


def generate():
    """Все версии заказов, одна строка на версию."""
    rng = np.random.default_rng(42)
    counts = daily_counts()
    n = sum(counts.values())

    starts = pd.DatetimeIndex(list(counts)).tz_localize(TZ).tz_convert("UTC")
    starts = starts.repeat(list(counts.values()))
    created = (starts + seconds(rng, 0, 86400, n)).floor("s").sort_values()

    df = pd.DataFrame({
        "order_id": [f"O{k:08d}" for k in range(1, n + 1)],
        "created_at": created,
        "customer_id": [f"C{k:06d}" for k in rng.integers(1, 30001, n)],
        "city": rng.choice(list(CITIES), n, p=list(CITIES.values())).astype(object),
        "status": rng.choice(list(STATUSES), n, p=list(STATUSES.values())),
        "amount_rub": np.clip(rng.lognormal(np.log(2500), 0.8, n), 100, 99000).round(2),
        "items_count": np.minimum(1 + rng.poisson(1.5, n), 50),
        "payment_method": rng.choice(list(PAYMENTS), n, p=list(PAYMENTS.values())),
    })
    df.loc[rng.random(n) < 0.003, "city"] = None
    df["updated_at"] = (df["created_at"] + seconds(rng, 60, 1800, n)).dt.floor("s")
    df["version"] = 1
    df["available_at"] = (df["updated_at"] + seconds(rng, 0, 600, n)).dt.floor("s")

    v2 = df[(df["status"] == "paid") & (rng.random(n) < 0.06)].copy()
    k = len(v2)
    v2["status"] = "refunded"
    v2["updated_at"] = (v2["updated_at"] + seconds(rng, 86400, 5 * 86400, k)).dt.floor("s")
    v2["version"] = 2
    v2["available_at"] = (v2["updated_at"] + seconds(rng, 0, 600, k)).dt.floor("s")

    cols = ["order_id", "created_at", "updated_at", "customer_id", "city", "status",
            "amount_rub", "items_count", "payment_method", "version", "available_at"]
    df = pd.concat([df, v2]).sort_values(["order_id", "version"])
    return df[cols].reset_index(drop=True)


def main():
    df = generate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"{OUT}: {df['order_id'].nunique()} заказов, {len(df)} версий")


if __name__ == "__main__":
    main()
