"""
Sanitizer for a raw Spiceworks ticket export.

Takes the full Spiceworks CSV (all 18 columns) and produces a masked,
analytics-ready CSV that is safe to commit/share:

- DROPS free-text and infrastructure fields entirely (Summary, Description,
  Link to Ticket, Organization Host) — these hold names, emails, internal IPs,
  hostnames, asset tags, and other sensitive detail.
- PSEUDONYMIZES people:
    * Assigned To  -> a stable tech alias (Tech A, Tech B, ... ; the portfolio
      owner can be pinned to a fixed alias via OWNER_NAME below).
    * Created By   -> a stable, non-reversible requester id (user_xxxxxxxx).
- KEEPS only structured, non-identifying fields useful for analysis.
- DERIVES a single `request_type` (Request Type, else shortened Category,
  else "Uncategorized") and `resolution_hours` from the timestamps.

Usage:
    python sanitize.py --input ticket-export.csv --output sample_data/tickets_masked.csv

Review the output by hand before committing. Automated scrubbing is a
starting point, not a guarantee.
"""

import argparse
import hashlib
import os

import pandas as pd

# If set, this exact "Assigned To" value is pinned to OWNER_ALIAS so the
# portfolio owner can highlight their own volume. Set to None to anonymize all.
# Currently None for full anonymity — set to "Davis Le" to re-pin the owner.
OWNER_NAME = None
OWNER_ALIAS = "Tech (me)"

# Columns that identify the organization are intentionally never read into the
# output. Listed here only to document the redaction.
NEVER_INCLUDE = ["Organization Name", "Organization Host", "Site / Office", "Link to Ticket"]

# New tagging taxonomy. ticket_categories.csv maps each ticket_id to a category
# + request type using these short codes; they expand to the full labels below.
CATEGORY_LABELS = {
    "ACCESS": "Account & Access",
    "CLOUD": "Cloud, DevOps & Platform",
    "DNS": "DNS & Domains",
    "EMAIL": "Email & Collaboration",
    "GENERAL": "General / Other",
    "HARDWARE": "Hardware & Peripherals",
    "MOBILE": "Mobile & Phone",
    "NETWORK": "Network & VPN",
    "SECURITY": "Security & Compliance",
    "SOFTWARE": "Software & Licensing",
}
TYPE_LABELS = {
    "INC": "Incident",
    "SR": "Service Request",
    "ONB": "Onboarding",
    "OFF": "Offboarding",
    "CHG": "Change Request",
}

DATE_FMT = "%m/%d/%Y %I:%M %p"

# Non-genuine tickets excluded from all analysis:
#   1, 2  -> Spiceworks' built-in welcome / import tutorial tickets
#   3, 6, 7 -> "Test ticket" / "Test" / "Testing"
# They were auto-created and closed years apart, producing meaningless
# resolution times that distort KPIs. Edit this set to change what's excluded.
EXCLUDE_TICKET_IDS = {1, 2, 3, 6, 7}


def parse_dt(series: pd.Series) -> pd.Series:
    # Strip a trailing " UTC" if present, then parse.
    cleaned = series.astype(str).str.replace(r"\s*UTC\s*$", "", regex=True).str.strip()
    return pd.to_datetime(cleaned, format=DATE_FMT, errors="coerce")


def requester_id(value: object) -> str:
    if pd.isna(value) or str(value).strip() == "":
        return "unknown"
    digest = hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"user_{digest}"


def build_tech_aliases(names: pd.Series) -> dict:
    """Stable Tech A/B/C... aliases, ordered by ticket volume (busiest = A)."""
    counts = names.fillna("Unassigned").value_counts()
    alias_map, letter = {}, ord("A")
    for name in counts.index:
        if OWNER_NAME and name == OWNER_NAME:
            alias_map[name] = OWNER_ALIAS
        else:
            alias_map[name] = f"Tech {chr(letter)}"
            letter += 1
    return alias_map


def canon_label(value: object) -> str | None:
    """Normalize a Category / Request Type cell to its canonical short label,
    stripping any "(...)" helper text — e.g.
    "Account & Access (logins, VPN, MFA, passwords)" -> "Account & Access",
    "Incident (something is broken or not working)" -> "Incident".
    Used as the fallback for tickets not in the historical retag map (e.g. new
    exports that already carry the current taxonomy in their own columns).
    """
    if pd.isna(value):
        return None
    s = str(value).split("(")[0].strip()
    return s or None


def load_retag(path: str) -> dict:
    """Load ticket_id -> (category_label, request_type_label) from ticket_categories.csv."""
    if not path or not os.path.exists(path):
        return {}
    rt = pd.read_csv(path)
    mapping = {}
    for _, r in rt.iterrows():
        cat = CATEGORY_LABELS.get(str(r["category"]).strip(), "General / Other")
        typ = TYPE_LABELS.get(str(r["request_type"]).strip(), "Service Request")
        mapping[r["ticket_id"]] = (cat, typ)
    return mapping


# Representative issue titles derived from the taxonomy. The original free-text
# summaries were dropped for privacy (names, emails, IPs, asset tags), so these
# give the ticket preview a readable "Issue" column without reintroducing PII.
ISSUE_LABELS = {
    "Account & Access": {
        "Incident": "Login / account access issue",
        "Service Request": "Access / account request",
        "Change Request": "Access change / group update",
    },
    "Cloud, DevOps & Platform": {
        "Incident": "Cloud service issue",
        "Service Request": "Cloud resource / API key request",
        "Change Request": "Cloud configuration change",
    },
    "DNS & Domains": {
        "Incident": "DNS / domain issue",
        "Service Request": "New DNS record request",
        "Change Request": "DNS record update",
    },
    "Email & Collaboration": {
        "Incident": "Email / Slack issue",
        "Service Request": "Email / group / alias request",
        "Change Request": "Distribution list / email change",
    },
    "General / Other": {
        "Incident": "General issue",
        "Service Request": "General request",
        "Change Request": "General change",
    },
    "Hardware & Peripherals": {
        "Incident": "Hardware failure / malfunction",
        "Service Request": "Hardware / peripheral request",
        "Change Request": "Hardware configuration change",
    },
    "Mobile & Phone": {
        "Incident": "Mobile / phone issue",
        "Service Request": "Phone / RingCentral setup",
        "Change Request": "Phone / RingCentral change",
    },
    "Network & VPN": {
        "Incident": "VPN / network connectivity issue",
        "Service Request": "VPN / network access request",
        "Change Request": "Network configuration change",
    },
    "Security & Compliance": {
        "Incident": "Security / antivirus issue",
        "Service Request": "Security access / tool request",
        "Change Request": "Security / access-control change",
    },
    "Software & Licensing": {
        "Incident": "Software / application issue",
        "Service Request": "Software install / license request",
        "Change Request": "Software / license change",
    },
}


def issue_label(category: str, request_type: str) -> str:
    if request_type == "Onboarding":
        return "New hire setup"
    if request_type == "Offboarding":
        return "Employee offboarding"
    return ISSUE_LABELS.get(category, {}).get(request_type, f"{category} — {request_type}")


def sanitize_dataframe(df: pd.DataFrame, retag: dict | None = None) -> pd.DataFrame:
    """Core sanitization: mask people, drop unsafe fields, apply the taxonomy.

    Takes a raw Spiceworks DataFrame and returns the safe, analytics-ready frame.
    Shared by the CLI (main) and the Streamlit dashboard so logic never drifts.
    """
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df[~df["Ticket Number"].isin(EXCLUDE_TICKET_IDS)]
    retag = retag or {}

    out = pd.DataFrame()
    out["ticket_id"] = df["Ticket Number"]
    out["created_at"] = parse_dt(df["Created On"])
    out["closed_at"] = parse_dt(df["Closed On"]) if "Closed On" in df else pd.NaT

    # Category + request type: prefer the historical map (ticket_categories.csv);
    # otherwise fall back to the export's own Category / Request Type columns — a new
    # export already uses the current taxonomy — normalizing away any "(...)" helper text.
    out["category"] = df["Ticket Number"].map(lambda tid: retag.get(tid, (None, None))[0])
    out["request_type"] = df["Ticket Number"].map(lambda tid: retag.get(tid, (None, None))[1])
    if "Category" in df.columns:
        out["category"] = out["category"].fillna(df["Category"].map(canon_label))
    if "Request Type" in df.columns:
        out["request_type"] = out["request_type"].fillna(df["Request Type"].map(canon_label))
    out["category"] = out["category"].fillna("General / Other")
    out["request_type"] = out["request_type"].fillna("Service Request")

    out["priority"] = df.get("Priority", "").astype(str).str.strip().str.lower()
    out["status"] = df.get("Status", "").astype(str).str.strip().str.lower()

    aliases = build_tech_aliases(df["Assigned To"])
    out["assignee"] = df["Assigned To"].fillna("Unassigned").map(aliases)
    out["requester"] = df["Created By"].apply(requester_id)

    out["resolution_hours"] = (
        (out["closed_at"] - out["created_at"]).dt.total_seconds() / 3600.0
    ).round(2)

    # Readable, privacy-safe issue title derived from the taxonomy.
    out["issue"] = [issue_label(c, t) for c, t in zip(out["category"], out["request_type"])]

    # Order columns: id, issue, then the rest.
    out = out[["ticket_id", "issue", "created_at", "closed_at", "category", "request_type",
               "priority", "status", "assignee", "requester", "resolution_hours"]]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitize a raw Spiceworks export")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--categories",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_categories.csv"),
        help="CSV mapping ticket_id -> category/request_type (the current taxonomy)",
    )
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    retag = load_retag(args.categories)
    out = sanitize_dataframe(df, retag)

    out.to_csv(args.output, index=False)
    retagged = sum(1 for t in df["Ticket Number"] if t in retag)
    print(f"Wrote {len(out)} sanitized rows to {args.output}")
    print(f"Excluded {len(EXCLUDE_TICKET_IDS)} non-genuine tickets (welcome/test).")
    print(f"Re-tagged {retagged}/{len(out)} tickets into the new taxonomy.")
    print(f"Masked {df['Created By'].nunique()} requesters and "
          f"{df['Assigned To'].nunique()} assignees.")
    print("Dropped: Summary, Description, Link to Ticket, Organization Host, emails, IPs.")


if __name__ == "__main__":
    main()
