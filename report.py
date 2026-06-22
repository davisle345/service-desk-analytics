"""
Automated support-performance report.

Generates a Markdown report of service KPIs for a reporting period, with
period-over-period deltas against the prior period. Defaults to **quarterly**
cadence — monthly is too noisy for a small org (a quarter aggregates enough
tickets to see real signal). Monthly is still available via --freq month.

Designed to be run on a schedule (Task Scheduler / cron) so reporting happens
automatically each quarter.

Usage:
    # latest quarter in the data (default)
    python report.py --input sample_data/tickets_masked.csv --out output

    # a specific quarter / month
    python report.py --input sample_data/tickets_masked.csv --period 2026Q2 --out output
    python report.py --input sample_data/tickets_masked.csv --freq month --period 2026-05 --out output

    # every period of the chosen cadence, one file each
    python report.py --input sample_data/tickets_masked.csv --all --out output
"""

import argparse
import os

import pandas as pd

import kpis

FREQ_COL = {"year": "created_year", "quarter": "created_quarter", "month": "created_month"}
FREQ_CODE = {"year": "Y", "quarter": "Q", "month": "M"}
FREQ_NOUN = {"year": "year", "quarter": "quarter", "month": "month"}
PERIODS_PER_YEAR = {"year": 1, "quarter": 4, "month": 12}


def period_label(period: pd.Period, freq: str) -> str:
    if freq == "year":
        return f"{period.year}"
    if freq == "quarter":
        return f"{period.year} Q{period.quarter}"
    return period.strftime("%B %Y")


def _pct_delta(curr, prev) -> str:
    if prev in (None, 0) or curr is None or pd.isna(prev) or pd.isna(curr):
        return "—"
    return f"{(curr - prev) / prev * 100:+.0f}%"


def period_report(df_all: pd.DataFrame, period_str: str, freq: str = "quarter",
                  sla_hours: dict | None = None) -> str:
    """Build a Markdown KPI report for one period (tickets created in it)."""
    df_all = kpis.prepare(df_all)
    col = FREQ_COL[freq]
    noun = FREQ_NOUN[freq]
    period = pd.Period(period_str, freq=FREQ_CODE[freq])

    cur = df_all[df_all[col] == period]
    prev = df_all[df_all[col] == (period - 1)]
    label = period_label(period, freq)

    if cur.empty:
        return f"# Support Performance Report — {label}\n\n_No tickets created in {label}._\n"

    vs = kpis.volume_summary(cur)
    rs = kpis.resolution_stats(cur)
    sla = kpis.overall_sla_pct(cur, sla_hours)
    pvs = kpis.volume_summary(prev) if not prev.empty else None
    prs = kpis.resolution_stats(prev) if not prev.empty else None
    psla = kpis.overall_sla_pct(prev, sla_hours) if not prev.empty else None

    L = [
        f"# Support Performance Report — {label}",
        "",
        f"_Cadence: {noun}ly · {vs['total']} tickets created this {noun}._",
        "",
        "## Headline KPIs",
        "",
        f"| KPI | This {noun} | vs prior {noun} |",
        "|---|---|---|",
        f"| Tickets created | {vs['total']} | {_pct_delta(vs['total'], pvs['total'] if pvs else None)} |",
        f"| Tickets closed | {vs['closed']} | {_pct_delta(vs['closed'], pvs['closed'] if pvs else None)} |",
        f"| Closure rate | {vs['closure_rate_pct']}% | "
        f"{_pct_delta(vs['closure_rate_pct'], pvs['closure_rate_pct'] if pvs else None)} |",
        f"| Median resolution (h) | {rs['median_hours']} | "
        f"{_pct_delta(rs['median_hours'], prs['median_hours'] if prs else None)} |",
        f"| SLA compliance | {sla if sla is not None else '—'}% | {_pct_delta(sla, psla)} |",
        "",
    ]

    sla_rows = kpis.sla_compliance(cur, sla_hours)
    if not sla_rows.empty:
        L += ["## SLA compliance by priority", "",
              "| Priority | Target (h) | Closed | Met | SLA % |", "|---|---|---|---|---|"]
        for _, r in sla_rows.iterrows():
            L.append(f"| {r['priority']} | {int(r['target_hours'])} | "
                     f"{int(r['closed'])} | {int(r['met'])} | {r['sla_pct']}% |")
        L.append("")

    cat = kpis.counts_by(cur, "category")
    L += ["## Volume by category", "", "| Category | Tickets | % |", "|---|---|---|"]
    for _, r in cat.iterrows():
        L.append(f"| {r['category']} | {r['tickets']} | {r['pct']}% |")
    L.append("")

    rt = kpis.counts_by(cur, "request_type")
    L += ["## Volume by request type", "", "| Request type | Tickets | % |", "|---|---|---|"]
    for _, r in rt.iterrows():
        L.append(f"| {r['request_type']} | {r['tickets']} | {r['pct']}% |")
    L.append("")

    rbc = kpis.resolution_by(cur, "category").head(5)
    if not rbc.empty:
        L += ["## Slowest categories (median resolution)", "",
              "| Category | Tickets | Median (h) |", "|---|---|---|"]
        for _, r in rbc.iterrows():
            L.append(f"| {r['category']} | {int(r['tickets'])} | {r['median_hours']} |")
        L.append("")

    top_cat, top_rt = cat.iloc[0], rt.iloc[0]
    L += ["## Summary & opportunities", ""]
    vol_note = ""
    if pvs:
        d = vs["total"] - pvs["total"]
        vol_note = (f" Volume {'rose' if d > 0 else 'fell' if d < 0 else 'held flat'}"
                    f" {abs(d)} tickets vs the prior {noun}.")
    L.append(f"- **{top_cat['category']}** led volume ({top_cat['pct']}%), and "
             f"**{top_rt['request_type']}** was the dominant request type ({top_rt['pct']}%).{vol_note}")
    if sla is not None and not sla_rows.empty:
        worst = sla_rows.sort_values("sla_pct").iloc[0]
        if worst["sla_pct"] < 100:
            L.append(f"- SLA compliance was **{sla}%** overall; **{worst['priority']}** priority was the "
                     f"weakest at {worst['sla_pct']}% — the area to watch.")
        else:
            L.append(f"- SLA compliance was **{sla}%** overall.")

    # Actionable ROI / time-savings estimate
    mult = PERIODS_PER_YEAR[freq]
    opp = kpis.opportunity(cur, periods_per_year=mult)
    if opp["repeatable"]:
        L.append(
            f"- **Automation opportunity:** {opp['repeatable']} of {opp['total']} tickets "
            f"({opp['repeatable_pct']:.0f}%) were repeatable Service/Change Requests — roughly "
            f"**{opp['tech_hours']} tech-hours** this {noun} (assuming ~{opp['handle_minutes']} min each). "
            f"Self-service or scripting that deflects ~{int(opp['deflect_pct']*100)}% could free "
            f"**~{opp['saved_hours']} hrs/{noun} (~{opp['saved_hours_annual']:.0f} hrs/year)**."
        )
        if opp["top_category"]:
            L.append(
                f"- **Best target:** **{opp['top_category']}** ({opp['top_category_n']} tickets) is the "
                "highest-volume category and the most templatable — a documented runbook or scripted "
                "workflow here yields the biggest, fastest win."
            )
    L.append(
        "- **Business impact:** this report is generated automatically from the ticket export, "
        f"replacing manual spreadsheet reporting and giving leadership a repeatable {noun}ly view of "
        "SLA, volume, backlog, and where to invest in automation."
    )

    return "\n".join(L) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Support-performance report (quarterly by default)")
    p.add_argument("--input", required=True)
    p.add_argument("--out", default="output")
    p.add_argument("--freq", choices=["year", "quarter", "month"], default="year",
                   help="Reporting cadence (default: year — best for a low-volume queue)")
    p.add_argument("--period", help="e.g. 2025, 2026Q2, or 2026-05 (defaults to the latest period)")
    p.add_argument("--all", action="store_true", help="Generate a report for every period")
    args = p.parse_args()

    os.makedirs(args.out, exist_ok=True)
    df = pd.read_csv(args.input)
    prepared = kpis.prepare(df)
    col = FREQ_COL[args.freq]
    periods = sorted(prepared[col].dropna().astype(str).unique())

    if args.all:
        for per in periods:
            md = period_report(df, per, args.freq)
            with open(os.path.join(args.out, f"report_{per}.md"), "w", encoding="utf-8") as f:
                f.write(md)
        print(f"Wrote {len(periods)} {args.freq}ly reports to {args.out}")
        return

    target = args.period or periods[-1]
    md = period_report(df, target, args.freq)
    path = os.path.join(args.out, f"report_{target}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Wrote {args.freq}ly report for {target} to {path}")


if __name__ == "__main__":
    main()
