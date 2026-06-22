"""
Service-desk KPIs
-----------------
Computes the standard help-desk reporting metrics from a masked ticket
DataFrame (the schema produced by sanitize.py):

    ticket_id, created_at, closed_at, category, request_type, priority,
    status, assignee, requester, resolution_hours

Everything here is pure pandas and returns plain DataFrames/dicts so it can be
reused by the CLI report generator (report.py) and the Streamlit dashboard.

KPIs covered:
- Volume (total / closed / open, by month, by category, by request type, by priority)
- Resolution time (median + mean overall and by priority / category)
- SLA compliance vs per-priority targets
- Throughput (created vs closed per month, net backlog change)
- Open backlog and ticket aging
- Workload distribution across (anonymized) technicians
"""

from __future__ import annotations

import pandas as pd

# Default SLA resolution targets in hours, by priority. Override per deployment.
DEFAULT_SLA_HOURS = {"urgent": 4, "high": 8, "medium": 24, "low": 72}

# Assumed average hands-on handling time per ticket, used for time-saved
# estimates (the raw export's "Time Spent" field is mostly blank). Adjust to
# match your team's reality.
AVG_HANDLE_MINUTES = 15

# Request types that are repeatable/templatable and thus good automation or
# self-service candidates.
REPEATABLE_TYPES = {"Service Request", "Change Request"}

OPEN_STATUSES = {"open", "waiting", "in progress", "new", "pending"}


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize types and add helper columns used across the KPIs."""
    df = df.copy()
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
    if "resolution_hours" not in df.columns and "closed_at" in df.columns:
        df["resolution_hours"] = (
            df["closed_at"] - df["created_at"]
        ).dt.total_seconds() / 3600.0
    df["priority"] = df.get("priority", "").astype(str).str.lower()
    df["status"] = df.get("status", "").astype(str).str.lower()
    df["is_closed"] = df["status"].eq("closed")
    df["created_month"] = df["created_at"].dt.to_period("M")
    df["created_quarter"] = df["created_at"].dt.to_period("Q")
    df["created_year"] = df["created_at"].dt.to_period("Y")
    if "closed_at" in df.columns:
        df["closed_month"] = df["closed_at"].dt.to_period("M")
    return df


def as_of(df: pd.DataFrame) -> pd.Timestamp:
    """Reference 'now' for aging — the latest timestamp present in the data."""
    candidates = [df["created_at"].max()]
    if "closed_at" in df.columns:
        candidates.append(df["closed_at"].max())
    return max(t for t in candidates if pd.notna(t))


def volume_summary(df: pd.DataFrame) -> dict:
    total = len(df)
    closed = int(df["is_closed"].sum())
    return {
        "total": total,
        "closed": closed,
        "open": total - closed,
        "closure_rate_pct": round(closed / total * 100, 1) if total else 0.0,
    }


def resolution_stats(df: pd.DataFrame) -> dict:
    res = df.loc[df["is_closed"], "resolution_hours"].dropna()
    if res.empty:
        return {"median_hours": None, "mean_hours": None, "p90_hours": None}
    return {
        "median_hours": round(float(res.median()), 1),
        "mean_hours": round(float(res.mean()), 1),
        "p90_hours": round(float(res.quantile(0.9)), 1),
    }


def resolution_by(df: pd.DataFrame, col: str) -> pd.DataFrame:
    g = (
        df[df["is_closed"]]
        .dropna(subset=["resolution_hours"])
        .groupby(col)["resolution_hours"]
        .agg(tickets="count", median_hours="median", mean_hours="mean")
        .reset_index()
        .sort_values("median_hours", ascending=False)
    )
    g["median_hours"] = g["median_hours"].round(1)
    g["mean_hours"] = g["mean_hours"].round(1)
    return g


def sla_compliance(df: pd.DataFrame, sla_hours: dict | None = None) -> pd.DataFrame:
    """% of closed tickets resolved within the per-priority SLA target."""
    sla_hours = sla_hours or DEFAULT_SLA_HOURS
    closed = df[df["is_closed"]].dropna(subset=["resolution_hours"]).copy()
    closed["target"] = closed["priority"].map(sla_hours)
    closed = closed.dropna(subset=["target"])
    closed["met"] = closed["resolution_hours"] <= closed["target"]
    rows = (
        closed.groupby("priority")
        .agg(target_hours=("target", "first"),
             closed=("met", "count"),
             met=("met", "sum"))
        .reset_index()
    )
    rows["sla_pct"] = (rows["met"] / rows["closed"] * 100).round(1)
    return rows


def overall_sla_pct(df: pd.DataFrame, sla_hours: dict | None = None) -> float | None:
    rows = sla_compliance(df, sla_hours)
    if rows.empty or rows["closed"].sum() == 0:
        return None
    return round(rows["met"].sum() / rows["closed"].sum() * 100, 1)


def counts_by(df: pd.DataFrame, col: str) -> pd.DataFrame:
    c = df[col].value_counts()
    out = c.rename_axis(col).reset_index(name="tickets")
    out["pct"] = (out["tickets"] / out["tickets"].sum() * 100).round(0).astype(int)
    return out


def throughput(df: pd.DataFrame) -> pd.DataFrame:
    """Created vs closed per month, with net backlog change."""
    created = df.groupby("created_month").size().rename("created")
    closed = (
        df[df["is_closed"]].groupby("closed_month").size().rename("closed")
        if "closed_month" in df.columns else pd.Series(dtype=int, name="closed")
    )
    out = pd.concat([created, closed], axis=1).fillna(0).astype(int)
    out["net_backlog_change"] = out["created"] - out["closed"]
    out.index = out.index.astype(str)
    return out.reset_index(names="month")


def backlog_aging(df: pd.DataFrame) -> pd.DataFrame:
    """Open tickets bucketed by age as of the latest date in the data."""
    ref = as_of(df)
    openi = df[~df["is_closed"]].copy()
    if openi.empty:
        return pd.DataFrame(columns=["age_bucket", "tickets"])
    age_days = (ref - openi["created_at"]).dt.total_seconds() / 86400.0
    buckets = pd.cut(
        age_days,
        bins=[-1, 7, 30, 90, float("inf")],
        labels=["0-7 days", "8-30 days", "31-90 days", "90+ days"],
    )
    out = buckets.value_counts().rename_axis("age_bucket").reset_index(name="tickets")
    return out


def workload(df: pd.DataFrame) -> pd.DataFrame:
    out = counts_by(df, "assignee").rename(columns={"assignee": "technician"})
    return out


def opportunity(df: pd.DataFrame, handle_minutes: int = AVG_HANDLE_MINUTES,
                deflect_pct: float = 0.4, periods_per_year: int = 4) -> dict:
    """Estimate the time-savings opportunity from automating/self-serving the
    repeatable part of the queue.

    Uses an assumed average handling time (AVG_HANDLE_MINUTES) since the export
    has no reliable per-ticket labor field. Returns rough, clearly-labeled
    estimates for an "opportunity" line in a report — not a billing figure.
    """
    n_total = len(df)
    n_rep = int(df["request_type"].isin(REPEATABLE_TYPES).sum())
    tech_hours = n_rep * handle_minutes / 60.0
    saved_hours = tech_hours * deflect_pct
    cats = df["category"].value_counts()
    return {
        "total": n_total,
        "repeatable": n_rep,
        "repeatable_pct": round(n_rep / n_total * 100) if n_total else 0,
        "handle_minutes": handle_minutes,
        "deflect_pct": deflect_pct,
        "tech_hours": round(tech_hours, 1),
        "saved_hours": round(saved_hours, 1),
        "saved_hours_annual": round(saved_hours * periods_per_year),
        "top_category": cats.index[0] if not cats.empty else None,
        "top_category_n": int(cats.iloc[0]) if not cats.empty else 0,
    }


def monthly_table(df: pd.DataFrame, sla_hours: dict | None = None) -> pd.DataFrame:
    """One row per month: created, closed, median resolution, SLA %."""
    rows = []
    for month, g in df.groupby("created_month"):
        vs = volume_summary(g)
        rs = resolution_stats(g)
        rows.append({
            "month": str(month),
            "created": vs["total"],
            "closed": vs["closed"],
            "median_res_hours": rs["median_hours"],
            "sla_pct": overall_sla_pct(g, sla_hours),
        })
    return pd.DataFrame(rows).sort_values("month").reset_index(drop=True)
