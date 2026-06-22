"""
Spiceworks Ticket Analytics
---------------------------
Load a sanitized/masked Spiceworks ticket dataset, store it in SQLite, and
write charts + a Markdown insights summary.

Expects the masked schema produced by sanitize.py:
    ticket_id, created_at, closed_at, category, request_type, priority,
    status, assignee, requester, resolution_hours

Usage:
    python analyze_tickets.py --input sample_data/tickets_masked.csv --out output
"""

import argparse
import glob
import os
import sqlite3

import pandas as pd
import matplotlib

matplotlib.use("Agg")  # no display needed; we write PNGs
import matplotlib.pyplot as plt

REQUIRED_COLUMNS = ["ticket_id", "created_at", "category", "request_type", "status"]


def load_and_clean(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input is missing required columns: {missing}")

    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")

    if "resolution_hours" not in df.columns and "closed_at" in df.columns:
        df["resolution_hours"] = (
            df["closed_at"] - df["created_at"]
        ).dt.total_seconds() / 3600.0

    df["created_month"] = df["created_at"].dt.to_period("M").dt.to_timestamp()
    return df


def to_sqlite(df: pd.DataFrame, db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        df.to_sql("tickets", conn, if_exists="replace", index=False)


def chart_volume_over_time(df: pd.DataFrame, out_dir: str) -> None:
    monthly = df.groupby("created_month").size()
    plt.figure(figsize=(10, 4))
    monthly.plot(kind="line", marker="o")
    plt.title("Ticket Volume by Month")
    plt.xlabel("Month")
    plt.ylabel("Tickets")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "volume_by_month.png"))
    plt.close()


def chart_counts(df: pd.DataFrame, col: str, title: str, fname: str, out_dir: str) -> pd.Series:
    counts = df[col].value_counts()
    plt.figure(figsize=(10, 5))
    counts.iloc[::-1].plot(kind="barh")
    plt.title(title)
    plt.xlabel("Tickets")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, fname))
    plt.close()
    return counts


def chart_mttr_by_category(df: pd.DataFrame, out_dir: str) -> pd.Series:
    if "resolution_hours" not in df.columns:
        return pd.Series(dtype=float)
    mttr = (
        df.dropna(subset=["resolution_hours"])
        .groupby("category")["resolution_hours"]
        .median()
        .sort_values(ascending=False)
    )
    plt.figure(figsize=(10, 5))
    mttr.iloc[::-1].plot(kind="barh", color="#c0504d")
    plt.title("Median Resolution Time by Category (hours)")
    plt.xlabel("Hours")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "mttr_by_category.png"))
    plt.close()
    return mttr


def chart_mttr_by_request_type(df: pd.DataFrame, out_dir: str) -> pd.Series:
    """Median resolution by the current request-type taxonomy (Incident, Service
    Request, Onboarding, Offboarding, Change Request)."""
    if "resolution_hours" not in df.columns:
        return pd.Series(dtype=float)
    mttr = (
        df.dropna(subset=["resolution_hours"])
        .groupby("request_type")["resolution_hours"]
        .median()
        .sort_values(ascending=False)
    )
    plt.figure(figsize=(10, 4))
    mttr.iloc[::-1].plot(kind="barh", color="#4f81bd")
    plt.title("Median Resolution Time by Request Type (hours)")
    plt.xlabel("Hours")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "mttr_by_request_type.png"))
    plt.close()
    return mttr


def _table(title, series, total, unit_col):
    lines = [f"## {title}", "", f"| {unit_col} | Tickets | % |", "|---|---|---|"]
    for k, n in series.items():
        lines.append(f"| {k} | {n} | {n / total * 100:.0f}% |")
    lines.append("")
    return lines


def write_summary(df, cat_counts, type_counts, mttr, out_dir):
    total = len(df)
    closed = int(df["status"].eq("closed").sum())
    open_like = total - closed
    top_cat = cat_counts.index[0]
    top_cat_pct = cat_counts.iloc[0] / total * 100
    top_type = type_counts.index[0]
    top_type_pct = type_counts.iloc[0] / total * 100

    res = df["resolution_hours"].dropna() if "resolution_hours" in df else pd.Series(dtype=float)
    median_res = res.median() if not res.empty else float("nan")
    span = f"{df['created_at'].min():%b %Y} – {df['created_at'].max():%b %Y}"

    lines = [
        "# Ticket Analytics Summary",
        "",
        f"- **Window:** {span}",
        f"- **Total tickets:** {total}  (**{closed}** closed, **{open_like}** open/other)",
        f"- **Top category:** {top_cat} ({top_cat_pct:.0f}%)",
        f"- **Top request type:** {top_type} ({top_type_pct:.0f}%)",
    ]
    if not res.empty:
        lines.append(f"- **Median resolution time:** {median_res:.1f} hours "
                     f"({median_res / 24:.1f} days)")
    lines.append("")

    lines += _table("Volume by category", cat_counts, total, "Category")
    lines += _table("Volume by request type", type_counts, total, "Request type")

    if not mttr.empty:
        lines += ["## Median resolution time by category (hours)", "",
                  "| Category | Median hrs |", "|---|---|"]
        for c, h in mttr.items():
            lines.append(f"| {c} | {h:.1f} |")
        lines.append("")

    lines += [
        "## Insights -> Actions",
        "",
        f"- **{top_cat}** is the largest category at {top_cat_pct:.0f}% — the clearest target "
        f"for templating, self-service, or automation.",
        f"- **{top_type}** is the dominant request type at {top_type_pct:.0f}%, which shapes how "
        f"the queue should be staffed and triaged.",
        "- High-median-time categories are runbook/SLA candidates: long tails usually mean "
        "external dependencies (vendor RMA, DNS propagation) rather than slow triage.",
        "",
        "_Charts: volume_by_month.png, by_category.png, by_request_type.png, "
        "mttr_by_category.png, mttr_by_request_type.png_",
    ]

    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Spiceworks ticket analytics")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", default="output")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    # Clear stale charts so old/renamed figures can't linger between runs.
    for png in glob.glob(os.path.join(args.out, "*.png")):
        os.remove(png)

    df = load_and_clean(args.input)
    to_sqlite(df, os.path.join(args.out, "tickets.db"))

    chart_volume_over_time(df, args.out)
    cat_counts = chart_counts(df, "category", "Tickets by Category", "by_category.png", args.out)
    type_counts = chart_counts(df, "request_type", "Tickets by Request Type", "by_request_type.png", args.out)
    mttr = chart_mttr_by_category(df, args.out)
    chart_mttr_by_request_type(df, args.out)
    write_summary(df, cat_counts, type_counts, mttr, args.out)

    print(f"Done. Analyzed {len(df)} tickets.")
    print(f"See {os.path.join(args.out, 'summary.md')} and the PNG charts.")


if __name__ == "__main__":
    main()
