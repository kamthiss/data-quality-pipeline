import datetime as dt
import math
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, model_validator

from dqpipe.source.generator import OUT, TZ

PAGE = 500
FIELDS = ["order_id", "created_at", "updated_at", "customer_id", "city", "status",
          "amount_rub", "items_count", "payment_method"]
Fault = Literal["clean", "duplicates", "api_500", "api_down", "partial_page",
                "missing_day", "schema_drift", "bad_values", "late_data", "volume_drop"]

orders = pd.DataFrame()
state = {
    "now": pd.Timestamp("2026-10-01T03:00:00+03:00"),
    "fault": "clean",
    "fault_date": None,
    "calls": 0,
}


@asynccontextmanager
async def lifespan(app):
    global orders
    orders = pd.read_parquet(OUT)
    orders["day"] = orders["created_at"].dt.tz_convert(TZ).dt.date
    yield


app = FastAPI(lifespan=lifespan)


class State(BaseModel):
    now: AwareDatetime
    fault: Fault = "clean"
    fault_date: dt.date | None = None

    @model_validator(mode="after")
    def need_fault_date(self):
        if self.fault != "clean" and self.fault_date is None:
            raise ValueError(f"fault_date обязателен для режима {self.fault}")
        return self


def fault_for(day):
    # late_data бьёт по другому дню и обрабатывается в visible
    if day == state["fault_date"] and state["fault"] != "late_data":
        return state["fault"]
    return "clean"


def late_hidden(day):
    fd = state["fault_date"]
    if state["fault"] != "late_data" or day != fd - dt.timedelta(days=2):
        return []
    if state["now"] >= pd.Timestamp(dt.datetime.combine(fd, dt.time(12))).tz_localize(TZ):
        return []
    ids = orders.loc[orders["day"] == day, "order_id"].unique()
    return np.random.default_rng(7).choice(ids, 40, replace=False)


def visible(day):
    df = orders[(orders["day"] == day) & (orders["available_at"] <= state["now"])]
    df = df[~df["order_id"].isin(late_hidden(day))]
    df = df.sort_values(["order_id", "version"]).drop_duplicates("order_id", keep="last")
    return df[FIELDS].reset_index(drop=True)


def with_duplicates(df):
    picks = np.random.default_rng(7).choice(df["order_id"], math.floor(len(df) * 0.015), replace=False)
    df = pd.concat([df, df[df["order_id"].isin(picks)]])
    return df.sort_values("order_id", kind="stable").reset_index(drop=True)


def with_bad_values(df):
    picks = np.random.default_rng(7).choice(df["order_id"], 8, replace=False)
    df = df.copy()
    neg = df["order_id"].isin(picks[:5])
    df.loc[neg, "amount_rub"] = -df.loc[neg, "amount_rub"]
    df.loc[df["order_id"].isin(picks[5:]), "status"] = "unknown"
    return df


def served(day, fault):
    df = visible(day)
    if fault == "missing_day":
        return df.iloc[0:0]
    if fault == "bad_values":
        return with_bad_values(df)
    if fault == "volume_drop":
        lost = np.random.default_rng(7).choice(df["order_id"], math.floor(len(df) * 0.4), replace=False)
        return df[~df["order_id"].isin(lost)]
    return df


def to_items(df):
    df = df.copy()
    for c in ("created_at", "updated_at"):
        df[c] = df[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict("records")


@app.get("/health")
def health():
    """Проверка, что API живо."""
    return {"status": "ok"}


@app.get("/orders")
def get_orders(day: dt.date = Query(alias="date"), page: int = Query(1, ge=1)):
    """Страница заказов, созданных в московский день."""
    fault = fault_for(day)
    if fault == "api_down":
        raise HTTPException(503)
    if fault == "api_500":
        state["calls"] += 1
        if state["calls"] <= 2:
            raise HTTPException(500)

    df = served(day, fault)
    if fault == "duplicates":
        df = with_duplicates(df)
    rows = df.iloc[(page - 1) * PAGE: page * PAGE]
    if fault == "partial_page" and page == 2:
        rows = rows.iloc[0:0]
    if fault == "schema_drift":
        rows = rows.rename(columns={"amount_rub": "amount"})
    return {"date": day, "page": page, "pages": math.ceil(len(df) / PAGE), "items": to_items(rows)}


@app.get("/orders/summary")
def get_summary(day: dt.date = Query(alias="date")):
    """Контрольный итог дня по последним видимым версиям."""
    fault = fault_for(day)
    if fault == "api_down":
        raise HTTPException(503)
    df = served(day, fault)
    return {
        "date": day,
        "orders": df["order_id"].nunique(),
        "amount_rub_total": round(float(df["amount_rub"].sum()), 2),
    }


@app.post("/admin/state")
def set_state(body: State):
    """Служебный: задать now и режим сбоя, сбросить счётчики."""
    state["now"] = pd.Timestamp(body.now).tz_convert("UTC")
    state["fault"] = body.fault
    state["fault_date"] = body.fault_date
    state["calls"] = 0
    return {"now": state["now"].isoformat(), "fault": body.fault, "fault_date": body.fault_date}
