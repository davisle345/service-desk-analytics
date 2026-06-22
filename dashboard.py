"""
Ticket Analytics Dashboard (Streamlit)
--------------------------------------
Drag-and-drop a Spiceworks export (raw or already-masked) and get an interactive
dashboard: KPIs, volume trend, category / request-type breakdowns, resolution
times, filters, and a generated insights summary — all computed locally.

Run:
    pip install -r requirements.txt
    streamlit run dashboard.py

Nothing is uploaded anywhere; the file is processed in-memory on your machine.
Raw exports are sanitized (people pseudonymized, free-text/infra fields dropped)
and re-tagged into the current taxonomy before anything is shown.
"""

import io
import os
import re

import altair as alt
import pandas as pd
import streamlit as st

import kpis
from report import period_report
from sanitize import sanitize_dataframe, load_retag

HERE = os.path.dirname(os.path.abspath(__file__))
RETAG_PATH = os.path.join(HERE, "ticket_categories.csv")
MASKED_SAMPLE = os.path.join(HERE, "sample_data", "tickets_masked.csv")
SAMPLE_EXPORT = os.path.join(HERE, "sample_data", "sample_spiceworks_export.csv")

st.set_page_config(page_title="IT Service Desk Analytics", layout="wide")

st.markdown(
    """
    <style>
      footer {visibility: hidden;}
      .block-container {padding-top: 2.5rem; padding-bottom: 3rem;
                        padding-left: 3rem; padding-right: 3rem; max-width: 1600px;}
      .app-title {font-size: 1.7rem; font-weight: 700; color: #F8FAFC;
                  letter-spacing: -0.02em; margin-bottom: 0;}
      .app-sub {color: #94A3B8; font-size: 0.95rem; margin-top: 2px;}
      .app-rule {height: 3px; width: 56px; background: #3B82F6; border-radius: 2px;
                 margin: 10px 0 4px 0;}
      [data-testid="stMetricValue"] {font-size: 1.55rem;}
      h2, h3 {letter-spacing: -0.01em;}
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
    if "resolution_hours" not in df.columns and "closed_at" in df.columns:
        df["resolution_hours"] = (
            df["closed_at"] - df["created_at"]
        ).dt.total_seconds() / 3600.0
    df["created_month"] = df["created_at"].dt.to_period("M").dt.to_timestamp()
    return df


@st.cache_data(show_spinner=False)
def load_dataframe(file_bytes: bytes | None) -> pd.DataFrame:
    """Return a clean, masked dataframe from an upload, or the bundled sample."""
    if file_bytes is None:
        return _coerce(pd.read_csv(MASKED_SAMPLE))

    raw = pd.read_csv(io.BytesIO(file_bytes))
    cols = {c.strip() for c in raw.columns}
    if "Ticket Number" in cols:          # raw Spiceworks export -> sanitize
        masked = sanitize_dataframe(raw, load_retag(RETAG_PATH))
    elif "ticket_id" in cols:            # already a masked dataset
        masked = raw
    else:
        raise ValueError(
            "Unrecognized CSV. Expected a raw Spiceworks export (with a "
            "'Ticket Number' column) or an already-masked dataset ('ticket_id')."
        )
    return _coerce(masked)


@st.cache_data(show_spinner=False)
def upload_diagnostics(file_bytes: bytes) -> dict:
    """Compare a raw upload to its sanitized output for the self-check panel."""
    raw = pd.read_csv(io.BytesIO(file_bytes))
    raw.columns = [c.strip() for c in raw.columns]
    is_raw = "Ticket Number" in raw.columns
    masked = load_dataframe(file_bytes)

    sensitive = ["Summary", "Description", "Link to Ticket", "Organization Host",
                 "Organization Name", "Site / Office", "Due On"]
    dropped = [c for c in sensitive if c in raw.columns]
    masked_people = [c for c in ["Created By", "Assigned To"] if c in raw.columns]

    blob = masked.astype(str).to_csv(index=False)
    emails = len(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", blob))
    ips = len(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", blob))

    return {
        "is_raw": is_raw,
        "rows_in": len(raw),
        "rows_out": len(masked),
        "dropped": dropped,
        "masked_people": masked_people,
        "emails": emails,
        "ips": ips,
    }


# --------------------------------------------------------------------------- #
# Sidebar — data source + filters
# --------------------------------------------------------------------------- #
st.sidebar.header("Data")
upload = st.sidebar.file_uploader("Upload Spiceworks CSV", type="csv")
st.sidebar.caption(
    "Raw exports are sanitized and re-tagged automatically. "
    "Leave empty to explore the bundled anonymized dataset."
)
try:
    with open(SAMPLE_EXPORT, "rb") as _f:
        st.sidebar.download_button(
            "Download sample CSV", _f.read(),
            file_name="sample_spiceworks_export.csv", mime="text/csv",
            width="stretch",
            help="A small example in Spiceworks export format — download it to see the "
                 "expected columns, then upload it to try the dashboard.",
        )
except OSError:
    pass

try:
    df = load_dataframe(upload.getvalue() if upload else None)
except Exception as e:  # noqa: BLE001 - surface parsing/sanitization errors to the user
    st.error(f"Could not process that file: {e}")
    st.stop()

if upload is not None:
    diag = upload_diagnostics(upload.getvalue())
    excluded = diag["rows_in"] - diag["rows_out"]
    clean = (diag["emails"] == 0 and diag["ips"] == 0)
    if diag["is_raw"]:
        st.sidebar.success(
            f"✓ Processed {diag['rows_in']} → {diag['rows_out']} rows · "
            f"{'no PII detected' if clean else '⚠ review output'}"
        )
        with st.sidebar.expander("Sanitization self-check", expanded=True):
            if excluded:
                st.markdown(f"- **Rows:** {diag['rows_in']} in → {diag['rows_out']} out "
                            f"({excluded} welcome/test excluded)")
            else:
                st.markdown(f"- **Rows:** {diag['rows_in']} in → {diag['rows_out']} out")
            if diag["dropped"]:
                st.markdown(f"- **Dropped {len(diag['dropped'])} free-text/infra column(s):** "
                            + ", ".join(diag["dropped"]))
            if diag["masked_people"]:
                st.markdown("- **Masked:** " + ", ".join(diag["masked_people"])
                            + " → hashed IDs / `Tech` aliases")
            mark = "Pass" if clean else "Review"
            st.markdown(f"- **PII scan of output:** {mark} — {diag['emails']} emails, "
                        f"{diag['ips']} IPs found")
            st.caption("Processed in-memory in your browser — the file is never uploaded or stored.")
    else:
        st.sidebar.info(f"Loaded an already-masked dataset ({diag['rows_out']} rows).")

st.sidebar.header("Filters")


def reset_filters():
    for key in ("ticket_date_range", "flt_categories", "flt_types"):
        st.session_state.pop(key, None)


st.sidebar.button("↺ Reset to all time", on_click=reset_filters, width="stretch")

cats = sorted(df["category"].dropna().unique())
types = sorted(df["request_type"].dropna().unique())
sel_cats = st.sidebar.multiselect("Category", cats, default=cats, key="flt_categories")
sel_types = st.sidebar.multiselect("Request type", types, default=types, key="flt_types")

valid_dates = df["created_at"].dropna()
min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
# No min_value/max_value: Streamlit's range picker validates against those bounds
# mid-selection and throws "outside allowed range". The filter below clamps anyway.
date_range = st.sidebar.date_input(
    "Created between",
    value=(min_d, max_d),
    key="ticket_date_range",
)
st.sidebar.caption("Tip: use ↺ Reset to all time to clear the date range and category filters.")

mask = df["category"].isin(sel_cats) & df["request_type"].isin(sel_types)
if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
    mask &= df["created_at"].between(start, end)
fdf = df[mask]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sorted_bar(series: pd.Series, value_title: str, color: str = "#3B82F6"):
    """Vertical bar chart sorted left→right, lowest→highest value."""
    d = series.rename_axis("label").reset_index(name="value")
    return (
        alt.Chart(d)
        .mark_bar(color=color)
        .encode(
            x=alt.X("label:N", title=None,
                    sort=alt.EncodingSortField(field="value", order="ascending"),
                    axis=alt.Axis(labelAngle=-40)),
            y=alt.Y("value:Q", title=value_title),
            tooltip=[alt.Tooltip("label:N", title=""), alt.Tooltip("value:Q", title=value_title)],
        )
        .properties(height=300, width="container")
    )


def pie_chart(series: pd.Series, title: str | None = None):
    """Donut chart for a small categorical split (e.g., priority mix)."""
    d = series.rename_axis("label").reset_index(name="value")
    return (
        alt.Chart(d)
        .mark_arc(innerRadius=55)
        .encode(
            theta=alt.Theta("value:Q", stack=True),
            color=alt.Color("label:N", title=title,
                            sort=alt.EncodingSortField(field="value", order="descending")),
            tooltip=[alt.Tooltip("label:N", title=""), alt.Tooltip("value:Q", title="Tickets")],
        )
        .properties(height=260, width="container")
    )


def share_table(series: pd.Series, label: str) -> pd.DataFrame:
    counts = series.value_counts()
    return pd.DataFrame({
        label: counts.index,
        "Tickets": counts.values,
        "%": (counts.values / counts.values.sum() * 100).round(0).astype(int),
    })


def insight_data(d: pd.DataFrame) -> dict:
    """Compute the pieces behind the executive summary."""
    n = len(d)
    cc = d["category"].value_counts()
    tc = d["request_type"].value_counts()
    ic = d["issue"].value_counts() if "issue" in d.columns else pd.Series(dtype=int)
    closed = int(d["status"].eq("closed").sum())
    res = d["resolution_hours"].dropna()
    median = res.median() if not res.empty else float("nan")
    m = d.dropna(subset=["resolution_hours"]).groupby("category")["resolution_hours"].median()
    slow_cat = m.idxmax() if not m.empty else None
    slow_h = m.max() if not m.empty else None
    span = f"{d['created_at'].min():%b %Y} – {d['created_at'].max():%b %Y}"
    years = max((d["created_at"].max() - d["created_at"].min()).days / 365.25, 0.5)
    opp = kpis.opportunity(d)
    annual_saved = opp["saved_hours"] / years

    top_cat, top_cat_pct = (cc.index[0], cc.iloc[0] / n * 100) if n else ("—", 0)
    top_type, top_type_pct = (tc.index[0], tc.iloc[0] / n * 100) if n else ("—", 0)
    top_issue, top_issue_pct = (ic.index[0], ic.iloc[0] / n * 100) if len(ic) else ("—", 0)

    findings = [
        f"The single most common ticket is **{top_issue}** (**{top_issue_pct:.0f}%** of volume) — "
        "the clearest candidate to streamline, template, or self-serve.",
        f"**{top_cat}** is the largest category at **{top_cat_pct:.0f}%**, and **{top_type}** is the "
        f"dominant request type (**{top_type_pct:.0f}%**) — the queue skews toward fulfilment over break/fix.",
        f"Closure rate is **{closed / n * 100:.0f}%** with a **{median:.1f}h** median resolution time.",
    ]
    if slow_cat:
        findings.append(f"**{slow_cat}** carries the longest median resolution (**{slow_h:.0f}h**), "
                        "typically external dependencies (DNS propagation, vendor RMA).")

    actions = [
        f"**Automate the #1 issue** — *{top_issue}* alone is {ic.iloc[0] if len(ic) else 0} tickets; "
        "a scripted workflow or self-service form removes most of it.",
        f"**Template the top category** — a runbook for **{top_cat}** is the biggest, fastest win.",
        f"**Deflect the repeatable queue** — ~{opp['repeatable']} Service/Change Requests; "
        f"~{int(opp['deflect_pct']*100)}% deflection is worth **~{annual_saved:.0f} hrs/year**.",
    ]
    return {
        "n": n, "closed": closed, "span": span, "median": median,
        "top_cat": top_cat, "top_cat_pct": top_cat_pct,
        "top_type": top_type, "top_type_pct": top_type_pct,
        "top_issue": top_issue, "top_issue_pct": top_issue_pct,
        "issues": ic, "annual_saved": annual_saved, "handle_minutes": opp["handle_minutes"],
        "repeatable": opp["repeatable"], "deflect_pct": opp["deflect_pct"],
        "tech_hours": opp["tech_hours"], "saved_hours": opp["saved_hours"], "years": years,
        "findings": findings, "actions": actions,
    }


def build_insights(d: pd.DataFrame) -> str:
    """Executive-summary markdown (used for the download)."""
    s = insight_data(d)
    lines = [
        "# Executive Summary — IT Ticket Analytics",
        "",
        f"**Scope:** {s['span']} · {s['n']} tickets ({s['closed']} closed)",
        "",
        f"The most common ticket is *{s['top_issue']}* ({s['top_issue_pct']:.0f}%); {s['top_cat']} "
        f"leads categories and automating repeatable work could save roughly "
        f"{s['annual_saved']:.0f} hours per year.",
        "",
        "## Top recurring issues",
        *[f"- {issue} — {cnt} tickets ({cnt / s['n'] * 100:.0f}%)"
          for issue, cnt in s["issues"].head(6).items()],
        "",
        "## Key findings",
        *[f"- {b}" for b in s["findings"]],
        "",
        "## Recommended actions",
        *[f"- {b}" for b in s["actions"]],
        "",
        "## How the savings estimate works",
        f"- **Deflection** = preventing a ticket from reaching a technician via self-service "
        "(portal, password self-reset, KB article) or automation (a script/workflow). A deflected "
        "ticket is work the team never performs.",
        f"- **Repeatable tickets:** {s['repeatable']} Service/Change Requests × ~{s['handle_minutes']} "
        f"min ÷ 60 = ~{s['tech_hours']:.0f} technician-hours over ~{s['years']:.1f} years.",
        f"- **Saved:** {s['tech_hours']:.0f} hrs × {s['deflect_pct']*100:.0f}% deflection "
        f"= ~{s['saved_hours']:.0f} hrs → ~{s['annual_saved']:.0f} hrs/year.",
        "## Caveats",
        f"- Handling time (~{s['handle_minutes']} min) and deflection ({s['deflect_pct']*100:.0f}%) "
        "are adjustable assumptions; the export has no logged labor time.",
        "- Issue titles are derived from the category + request-type taxonomy (structural, not "
        "text-mined free text).",
        "- Low-volume queue — findings are directional, not statistically robust.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Header + global KPI strip (always visible above the tabs)
# --------------------------------------------------------------------------- #
st.markdown('<div class="app-title">IT Service Desk Analytics</div>'
            '<div class="app-rule"></div>'
            '<div class="app-sub">Ticket volume, SLA performance, and automation opportunities · '
            'anonymized demo data</div>', unsafe_allow_html=True)
st.write("")

if fdf.empty:
    st.warning("No tickets match the current filters.")
    st.stop()

total = len(fdf)
closed = int(fdf["status"].eq("closed").sum())
res = fdf["resolution_hours"].dropna()
median_res = res.median() if not res.empty else float("nan")

k = st.columns(6)
k[0].metric("Tickets", f"{total:,}")
k[1].metric("Closed", f"{closed:,}")
k[2].metric("Closure rate", f"{closed / total * 100:.0f}%",
            help="Share of tickets currently in view that are closed.")
k[3].metric("Median resolution", f"{median_res:.1f} h" if pd.notna(median_res) else "—",
            help="Median time from creation to close, across closed tickets.")
k[4].metric("Top category", fdf["category"].value_counts().idxmax())
k[5].metric("Top request type", fdf["request_type"].value_counts().idxmax())

tab_overview, tab_kpis, tab_report, tab_data = st.tabs(
    ["Overview", "Service KPIs", "Performance Report", "Data & Exports"]
)


# --------------------------------------------------------------------------- #
# Tab 1 — Overview (volume + breakdowns)
# --------------------------------------------------------------------------- #
with tab_overview:
    st.subheader("Volume by month")
    vol = fdf.groupby("created_month").size().reset_index(name="tickets")
    vol_line = (
        alt.Chart(vol)
        .mark_line(point=True, color="#3B82F6")
        .encode(
            x=alt.X("created_month:T", title=None),
            y=alt.Y("tickets:Q", title="Tickets"),
            tooltip=[alt.Tooltip("created_month:T", title="Month"),
                     alt.Tooltip("tickets:Q", title="Tickets")],
        )
        .properties(height=300, width="container")
    )
    st.altair_chart(vol_line)

    g1, g2 = st.columns(2)
    with g1:
        st.subheader("Tickets by category")
        st.altair_chart(sorted_bar(fdf["category"].value_counts(), "Tickets"))
        st.subheader("Median resolution by category (h)")
        st.altair_chart(sorted_bar(
            fdf.dropna(subset=["resolution_hours"]).groupby("category")["resolution_hours"].median(),
            "Hours", color="#F87171"))
    with g2:
        st.subheader("Tickets by request type")
        st.altair_chart(sorted_bar(fdf["request_type"].value_counts(), "Tickets"))
        st.subheader("Median resolution by request type (h)")
        st.altair_chart(sorted_bar(
            fdf.dropna(subset=["resolution_hours"]).groupby("request_type")["resolution_hours"].median(),
            "Hours", color="#F87171"))

    b1, b2 = st.columns(2)
    b1.subheader("Category breakdown")
    b1.dataframe(share_table(fdf["category"], "Category"), hide_index=True, width="stretch")
    b2.subheader("Request-type breakdown")
    b2.dataframe(share_table(fdf["request_type"], "Request type"), hide_index=True, width="stretch")


# --------------------------------------------------------------------------- #
# Tab 2 — Service KPIs
# --------------------------------------------------------------------------- #
with tab_kpis:
    kdf = kpis.prepare(fdf)
    rstats = kpis.resolution_stats(kdf)
    vstats = kpis.volume_summary(kdf)
    overall_sla = kpis.overall_sla_pct(kdf)
    backlog = kdf.loc[~kdf["is_closed"]].shape[0]

    m = st.columns(4)
    m[0].metric("SLA compliance", f"{overall_sla}%" if overall_sla is not None else "—",
                help="Share of closed tickets resolved within their priority's SLA target "
                     "(high 8h, medium 24h, low 72h).")
    m[1].metric("Median resolution", f"{rstats['median_hours']} h" if rstats['median_hours'] else "—",
                help="Median creation-to-close time. Robust to a few very slow tickets.")
    m[2].metric("Closure rate", f"{vstats['closure_rate_pct']:.0f}%",
                help="Closed tickets as a share of total.")
    m[3].metric("Open backlog", f"{backlog}", help="Tickets not yet closed.")

    kc1, kc2 = st.columns(2)
    with kc1:
        st.subheader("SLA compliance by priority")
        sla_tbl = kpis.sla_compliance(kdf)
        if sla_tbl.empty:
            st.caption("No closed tickets with a recognized priority in range.")
        else:
            st.dataframe(sla_tbl.rename(columns={
                "priority": "Priority", "target_hours": "Target (h)",
                "closed": "Closed", "met": "Met", "sla_pct": "SLA %",
            }), hide_index=True, width="stretch")

        st.subheader("Resolution by priority (hours)")
        st.dataframe(kpis.resolution_by(kdf, "priority").rename(columns={
            "priority": "Priority", "tickets": "Tickets",
            "median_hours": "Median", "mean_hours": "Mean",
        }), hide_index=True, width="stretch")

        st.subheader("Priority mix")
        st.altair_chart(pie_chart(fdf["priority"].str.title().value_counts(), "Priority"))
    with kc2:
        st.subheader("Throughput — created vs closed")
        tp = kpis.throughput(kdf)[["month", "created", "closed"]].melt(
            "month", var_name="metric", value_name="tickets")
        tp_line = (
            alt.Chart(tp)
            .mark_line(point=True)
            .encode(
                x=alt.X("month:N", title=None),
                y=alt.Y("tickets:Q", title="Tickets"),
                color=alt.Color("metric:N", title=None),
                tooltip=["month", "metric", "tickets"],
            )
            .properties(height=300, width="container")
        )
        st.altair_chart(tp_line)
        st.subheader("Open backlog by age")
        aging = kpis.backlog_aging(kdf)
        if aging.empty:
            st.caption("No open tickets in range.")
        else:
            order = ["0-7 days", "8-30 days", "31-90 days", "90+ days"]
            chart = (
                alt.Chart(aging)
                .mark_bar(color="#3B82F6")
                .encode(
                    x=alt.X("age_bucket:N", title=None, sort=order),
                    y=alt.Y("tickets:Q", title="Tickets"),
                    tooltip=["age_bucket", "tickets"],
                )
                .properties(height=300, width="container")
            )
            st.altair_chart(chart)


# --------------------------------------------------------------------------- #
# Tab 3 — Performance report (quarterly by default)
# --------------------------------------------------------------------------- #
with tab_report:
    st.caption(
        "Read top-down: headline KPIs vs the prior period → **What to watch** and "
        "**Recommended actions** → supporting detail. Annual cadence suits a low-volume queue."
    )
    top = st.columns([1, 1, 2])
    freq = top[0].radio("Cadence", ["year", "quarter", "month"], horizontal=True, index=0,
                        help="Annual is best here (~8 tickets/month). Quarter/month add detail "
                             "but get sparse fast.")
    fmap = {"year": ("created_year", "Y"), "quarter": ("created_quarter", "Q"),
            "month": ("created_month", "M")}
    pcol, fcode = fmap[freq]
    noun = freq
    kp = kpis.prepare(df)
    periods = sorted(kp[pcol].dropna().astype(str).unique())
    sel_period = top[1].selectbox("Period", periods, index=len(periods) - 1)

    period_obj = pd.Period(sel_period, freq=fcode)
    pdf = kp[kp[pcol] == period_obj]
    ppdf = kp[kp[pcol] == (period_obj - 1)]

    vs = kpis.volume_summary(pdf)
    rs = kpis.resolution_stats(pdf)
    sla = kpis.overall_sla_pct(pdf)
    pvs = kpis.volume_summary(ppdf) if len(ppdf) else None
    psla = kpis.overall_sla_pct(ppdf) if len(ppdf) else None
    sla_tbl = kpis.sla_compliance(pdf)
    opp = kpis.opportunity(pdf, periods_per_year={"year": 1, "quarter": 4, "month": 12}[freq])
    slow = kpis.resolution_by(pdf, "category").head(5)
    worst = sla_tbl.sort_values("sla_pct").iloc[0] if not sla_tbl.empty else None
    span = f"{pdf['created_at'].min():%b %d, %Y} – {pdf['created_at'].max():%b %d, %Y}"
    st.caption(f"**{sel_period}** · {len(pdf)} tickets · {span}")

    def _pct(curr, prev):
        if not prev or curr is None:
            return None
        return f"{(curr - prev) / prev * 100:+.0f}% vs prior {noun}"

    mc = st.columns(5)
    mc[0].metric("Created", vs["total"], _pct(vs["total"], pvs["total"] if pvs else None))
    mc[1].metric("Closed", vs["closed"], _pct(vs["closed"], pvs["closed"] if pvs else None))
    mc[2].metric("Closure rate", f"{vs['closure_rate_pct']:.0f}%")
    mc[3].metric("Median res", f"{rs['median_hours']} h" if rs["median_hours"] else "—")
    mc[4].metric("SLA compliance", f"{sla}%" if sla is not None else "—", _pct(sla, psla))

    # What to watch / Recommended actions
    watch, actions = [], []
    if len(pdf) < 30:
        watch.append(f"**Small sample** — only {len(pdf)} tickets this {noun}; read trends with "
                     "caution (annual cadence is steadier for this queue).")
    if sla is not None and sla < 90:
        msg = f"**SLA at {sla}%**, below a 90% target."
        if worst is not None:
            msg += f" Weakest: **{worst['priority']}** ({worst['sla_pct']}%)."
        watch.append(msg)
    open_n = vs["total"] - vs["closed"]
    if open_n:
        watch.append(f"**{open_n} ticket(s) still open** from this {noun}.")
    if not slow.empty and slow.iloc[0]["median_hours"] >= 48:
        s = slow.iloc[0]
        watch.append(f"**{s['category']}** resolves slowly (median {s['median_hours']} h) — "
                     "usually external dependencies (DNS propagation, vendor RMA).")

    if opp["repeatable"]:
        actions.append(f"**Automate the repeatable work** — {opp['repeatable']} Service/Change Requests "
                       f"(~{opp['tech_hours']} tech-hrs). Deflecting ~{int(opp['deflect_pct']*100)}% "
                       f"≈ **{opp['saved_hours_annual']:.0f} hrs/yr** saved.")
    if opp["top_category"]:
        actions.append(f"**Template the top category** — **{opp['top_category']}** "
                       f"({opp['top_category_n']} tickets); a scripted workflow/runbook is the biggest win.")
    if sla is not None and sla < 90 and worst is not None:
        actions.append(f"**Tighten triage on {worst['priority']}-priority tickets** to lift SLA.")

    w, a = st.columns(2)
    with w:
        st.markdown("#### What to watch")
        if watch:
            st.warning("\n\n".join(f"- {x}" for x in watch))
        else:
            st.success("Healthy — nothing flagged this period.")
    with a:
        st.markdown("#### Recommended actions")
        st.success("\n\n".join(f"- {x}" for x in actions)
                   if actions else "- Maintain current process; no pressing action.")
    st.caption(f"Savings estimate assumes ~{opp['handle_minutes']} min average handling per ticket.")

    # Supporting detail
    st.divider()
    d_left, d_right = st.columns(2)
    with d_left:
        st.subheader("Priority mix")
        st.altair_chart(pie_chart(pdf["priority"].str.title().value_counts(), "Priority"))
        st.subheader("SLA compliance by priority")
        if sla_tbl.empty:
            st.caption("No closed tickets with a recognized priority this period.")
        else:
            st.dataframe(sla_tbl.rename(columns={
                "priority": "Priority", "target_hours": "Target (h)",
                "closed": "Closed", "met": "Met", "sla_pct": "SLA %",
            }), hide_index=True, width="stretch")
    with d_right:
        st.subheader("Volume by category")
        st.dataframe(share_table(pdf["category"], "Category"), hide_index=True, width="stretch")
        st.subheader("Volume by request type")
        st.dataframe(share_table(pdf["request_type"], "Request type"), hide_index=True, width="stretch")

    report_md = period_report(df, sel_period, freq)
    st.download_button(
        "Download full report (Markdown)",
        report_md.encode("utf-8"),
        file_name=f"report_{sel_period}.md",
        mime="text/markdown",
    )


# --------------------------------------------------------------------------- #
# Tab 4 — Data & exports
# --------------------------------------------------------------------------- #
with tab_data:
    s = insight_data(fdf)
    st.subheader("Executive summary")
    st.caption(f"Scope: {s['span']} · {s['n']} tickets ({s['closed']} closed)")
    st.markdown(
        f"The most common ticket is **{s['top_issue']}** (**{s['top_issue_pct']:.0f}%** of volume); "
        f"**{s['top_cat']}** leads categories, and automating the repeatable work could save roughly "
        f"**{s['annual_saved']:.0f} hours/year**."
    )

    h = st.columns(4)
    h[0].metric("Tickets in view", f"{s['n']:,}")
    h[1].metric("Most common issue", s["top_issue"], f"{s['top_issue_pct']:.0f}% of volume",
                delta_color="off")
    h[2].metric("Median resolution", f"{s['median']:.1f} h")
    h[3].metric("Est. savings", f"~{s['annual_saved']:.0f} h/yr",
                help=f"Estimated technician hours/year that could be freed by self-service or "
                     f"automation. See 'How the savings estimate works' below.")

    with st.expander("How the savings estimate works (and what 'deflection' means)"):
        st.markdown(
            f"""
**Deflection** means stopping a ticket from ever reaching a technician — either by letting the user
solve it themselves (a self-service portal, a password self-reset, a knowledge-base article) or by
automating the task (a script or workflow that completes it with no manual effort). A *deflected*
ticket is work the team never has to do.

**How the estimate is built for the tickets currently in view:**

1. **Repeatable tickets:** **{s['repeatable']}** — the Service Requests + Change Requests. These are
   routine, fulfillable work (access requests, DNS changes, installs) that are realistic to template,
   self-serve, or script — unlike one-off incidents.
2. **Handling time:** ~**{s['handle_minutes']} min** per ticket. This is an *assumption* — Spiceworks
   didn't log actual labor time — and can be changed in `kpis.AVG_HANDLE_MINUTES`.
3. **Technician hours on that work:** {s['repeatable']} × {s['handle_minutes']} min ÷ 60 =
   **{s['tech_hours']:.0f} hrs** across the ~{s['years']:.1f}-year window.
4. **Deflection rate:** **{s['deflect_pct']*100:.0f}%** — a conservative estimate of how many of those
   repeatable tickets could be removed via self-service/automation.
5. **Hours saved (window):** {s['tech_hours']:.0f} × {s['deflect_pct']*100:.0f}% =
   **{s['saved_hours']:.0f} hrs**.
6. **Per year:** {s['saved_hours']:.0f} ÷ {s['years']:.1f} yrs ≈ **{s['annual_saved']:.0f} hrs/year**.

These are **directional estimates to size the opportunity**, not a guarantee. Two inputs drive them —
average handling time and deflection rate — and both are easy to adjust to match your team's reality.
"""
        )

    f_col, a_col = st.columns(2)
    with f_col:
        with st.container(border=True):
            st.markdown("##### Key findings")
            for b in s["findings"]:
                st.markdown(f"- {b}")
    with a_col:
        with st.container(border=True):
            st.markdown("##### Recommended actions")
            for b in s["actions"]:
                st.markdown(f"- {b}")

    st.caption("Low-volume queue — findings are directional, not statistically robust. "
               "Time-savings are estimates against a configurable handling-time assumption.")

    st.divider()
    st.markdown("##### Top recurring issues")
    st.caption("A more specific lens than category alone. Issue titles are derived from the "
               "category + request-type taxonomy (structural, not text-mined free text).")
    issue_counts = fdf["issue"].value_counts().head(8)
    st.altair_chart(sorted_bar(issue_counts, "Tickets"))

    st.divider()
    st.markdown("##### Exports")
    d1, d2 = st.columns(2)
    d1.download_button(
        "Download masked dataset (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        file_name="tickets_masked.csv",
        mime="text/csv",
        width="stretch",
    )
    d2.download_button(
        "Download executive summary (Markdown)",
        build_insights(fdf).encode("utf-8"),
        file_name="executive_summary.md",
        mime="text/markdown",
        width="stretch",
    )
    st.divider()
    st.subheader("Ticket preview")
    st.caption("Issue titles are derived from the category + request type — original ticket text "
               "was removed during sanitization for privacy.")
    pref = ["ticket_id", "issue", "category", "request_type", "priority",
            "status", "created_at", "closed_at", "resolution_hours", "assignee"]
    cols = [c for c in pref if c in fdf.columns]
    st.dataframe(
        fdf[cols], hide_index=True, width="stretch",
        column_config={
            "ticket_id": st.column_config.NumberColumn("ID", format="%d"),
            "issue": st.column_config.TextColumn("Issue", width="large"),
            "category": "Category",
            "request_type": "Request type",
            "priority": "Priority",
            "status": "Status",
            "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD"),
            "closed_at": st.column_config.DatetimeColumn("Closed", format="YYYY-MM-DD"),
            "resolution_hours": st.column_config.NumberColumn("Res (h)", format="%.1f"),
            "assignee": "Assignee",
        },
    )
