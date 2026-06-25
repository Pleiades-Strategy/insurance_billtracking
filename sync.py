#!/usr/bin/env python3
"""
Airtable → data.json sync script for the Insurance Bill Tracker.

Environment variables required (set as GitHub Actions secrets):
  AIRTABLE_KEY      — your Airtable Personal Access Token
  AIRTABLE_BASE_ID  — the base ID (e.g. appMLqvmasfgRiqrm)

Airtable field names used
  Insurance Bills: Bill Title, Bill Number, Bill Progress, Year, Category,
                   Notes, Last Action Date, BT50 URL, Summary, Sponsor Names,
                   Bill Last Action, Document Link, Bill Added Date, Number of Votes
  Legislators:     Name, BT50 Party, Legislator Title, District,
                   In Office?, Ballotpedia URL, FTM URL, BT50 ID,
                   2026 Insurance Bills  (linked record → bill IDs)
                   NOTE: State is derived from linked bills, not from the
                   "State (from …)" lookup field, because that lookup returns
                   Airtable record IDs (not state abbreviations) when the
                   underlying field is itself a linked record to the States table.
  Hearings:        Title, Start, End, Location, Description, BillID,
                   Hangouts Link, 2026 Insurance Bills (linked record → bill IDs)
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AIRTABLE_KEY     = os.environ.get("AIRTABLE_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")

if not AIRTABLE_KEY or not AIRTABLE_BASE_ID:
    print("ERROR: AIRTABLE_KEY and AIRTABLE_BASE_ID must be set.", file=sys.stderr)
    sys.exit(1)

HEADERS  = {"Authorization": f"Bearer {AIRTABLE_KEY}"}
BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"


# ---------------------------------------------------------------------------
# Airtable fetching
# ---------------------------------------------------------------------------
def fetch_table(table_name: str) -> list[dict]:
    """Fetch every record from a table, following Airtable pagination."""
    url     = f"{BASE_URL}/{requests.utils.quote(table_name)}"
    records: list[dict] = []
    params: dict = {}
    while True:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        records.extend(body.get("records", []))
        if "offset" not in body:
            break
        params["offset"] = body["offset"]
    print(f"  {table_name}: {len(records)} records")
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fget(fields: dict, *keys, default=None):
    """Return the first matching key's value; fall back to default."""
    for k in keys:
        if k in fields:
            return fields[k]
    return default


def to_list(val) -> list:
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def extract_state(title: str) -> str:
    """Pull the 2-letter state code from a title like 'CA SB123 (2025)'."""
    m = re.match(r"^([A-Z]{2})\s", title or "")
    return m.group(1) if m else ""


def status_bucket(status: str) -> str:
    s = (status or "").lower()
    if any(x in s for x in ("sign", "enact", "adopt", "passed")):
        return "enacted"
    if any(x in s for x in ("dead", "fail", "veto", "indef", "strip")):
        return "dead"
    return "active"


def linked_ids(fields: dict) -> list[str]:
    """
    Collect Airtable record IDs from both year-specific linked fields.
    Linked-record fields in the API always return an array of record-ID
    strings (e.g. ["recXXX", "recYYY"]).
    """
    return (
        to_list(fget(fields, "2026 Insurance Bills")) +
        to_list(fget(fields, "2025 Insurance Bills"))
    )


# ---------------------------------------------------------------------------
# Transformers
# ---------------------------------------------------------------------------
def transform_bills(records: list[dict]) -> list[dict]:
    bills = []
    for rec in records:
        f = rec.get("fields", {})
        title = fget(f, "Bill Title", default="")
        state = extract_state(title)

        year_raw = to_list(fget(f, "Year"))
        year     = [str(y) for y in year_raw if y]
        years_sorted = sorted(year)
        if len(years_sorted) >= 2:
            session = f"{years_sorted[0]}-{years_sorted[-1]} Regular Session"
        elif years_sorted:
            session = f"{years_sorted[0]} Session"
        else:
            session = ""

        keyword_tags = [c for c in to_list(fget(f, "Category")) if isinstance(c, str)]
        raw_status   = fget(f, "Bill Progress", default="")

        bills.append({
            "bill_id":          rec["id"],   # Airtable record ID — used for linking
            "state":            state,
            "bill_number":      fget(f, "Bill Number",    default=""),
            "title":            title,
            "status":           raw_status,
            "status_bucket":    status_bucket(raw_status),
            "session":          session,
            "introduced_date":  fget(f, "Bill Added Date",    default=""),
            "last_action_date": fget(f, "Last Action Date",   default=""),
            "last_action":      fget(f, "Bill Last Action",   default=""),
            "bill_url":         fget(f, "BT50 URL",           default=""),
            "document_link":    fget(f, "Document Link",      default=""),
            "keyword_tags":     keyword_tags,
            "summary":          fget(f, "Summary",            default=""),
            "notes":            fget(f, "Notes",              default=""),
            "year":             year,
            "sponsors":         fget(f, "Sponsor Names", "Name", default=""),
            "number_of_votes":  fget(f, "Number of Votes", default=0),
        })
    return bills


def transform_legislators(
    records: list[dict],
    bill_state_lookup: dict[str, str],
) -> list[dict]:
    """
    State resolution strategy:
      The "State (from 2026 Insurance Bills)" lookup field in Airtable returns
      Airtable record IDs (not state abbreviations) because the underlying
      "State" field in Insurance Bills is itself a linked record to the States
      table.  Instead, we derive state from the legislator's linked bill IDs —
      we already have a {bill_id → state} dict built from the bills we fetched.

    Field mapping:
      Name              → legislator_name  (includes party, e.g. "Ben Allen (D)")
      BT50 Party        → legislator_party
      Legislator Title  → role
      District          → district
      In Office?        → in_office
      Ballotpedia URL   → ballotpedia_url
      FTM URL           → follow_the_money_url
      BT50 ID           → bt50_id
      2026 Insurance Bills → linked_bill_ids  (for bill/hearing matching)
    """
    legislators = []
    for rec in records:
        f = rec.get("fields", {})
        bill_ids = linked_ids(f)

        # Derive state from the first linked bill that has a known state
        state = ""
        for bid in bill_ids:
            s = bill_state_lookup.get(bid, "")
            if s:
                state = s
                break

        legislators.append({
            "legislator_name":      fget(f, "Name", default=""),
            "legislator_party":     fget(f, "BT50 Party", "Party", default=""),
            "role":                 fget(f, "Legislator Title", "Role", default=""),
            "district":             fget(f, "District", default=""),
            "state_code":           state,
            "in_office":            bool(fget(f, "In Office?", "In Office", default=True)),
            "ballotpedia_url":      fget(f, "Ballotpedia URL", "Ballotpedia", default=""),
            "follow_the_money_url": fget(f, "FTM URL", "Follow the Money URL", default=""),
            "bt50_id":              fget(f, "BT50 ID", default=None),
            "bill_count":           len(bill_ids),
            "linked_bill_ids":      bill_ids,
        })
    return legislators


def transform_hearings(records: list[dict]) -> list[dict]:
    """
    Field mapping:
      Title            → title
      Start            → start_dt
      End              → end_dt
      Location         → location
      Description      → description
      BillID           → bill_id_numeric  (BillTrack50 numeric ID)
      Hangouts Link    → event_link
      2026 Insurance Bills → linked_bill_ids  (for bill matching)
    """
    hearings = []
    for rec in records:
        f = rec.get("fields", {})
        bill_ids = linked_ids(f)

        hearings.append({
            "bill_id_numeric": fget(f, "BillID", "Bill ID",  default=""),
            "title":           fget(f, "Title",  "Name",     default=""),
            "start_dt":        fget(f, "Start",  "Start Date", default=""),
            "end_dt":          fget(f, "End",    "End Date",   default=""),
            "status":          fget(f, "Status",              default=""),
            "location":        fget(f, "Location", "Committee", default=""),
            "description":     fget(f, "Description",         default=""),
            "event_link":      fget(f, "Hangouts Link", "Event Link", "URL", default=""),
            "state":           fget(f, "State",               default=""),
            "bill_number":     fget(f, "Bill Number",         default=""),
            "bill_title":      fget(f, "Bill Title",          default=""),
            "linked_bill_ids": bill_ids,
        })
    return hearings


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def compute_summary(bills: list[dict]) -> dict:
    total   = len(bills)
    states  = len({b["state"] for b in bills if b["state"]})
    enacted = sum(1 for b in bills if b["status_bucket"] == "enacted")
    dead    = sum(1 for b in bills if b["status_bucket"] == "dead")
    active  = total - enacted - dead
    with_text = sum(1 for b in bills if b.get("summary") or b.get("document_link"))
    return {"total": total, "states": states, "enacted": enacted,
            "active": active, "dead": dead, "with_text": with_text}


def compute_charts(bills: list[dict]) -> dict:
    status_c: dict = defaultdict(int)
    state_c:  dict = defaultdict(int)
    cat_c:    dict = defaultdict(int)
    for b in bills:
        if b["status"]: status_c[b["status"]] += 1
        if b["state"]:  state_c[b["state"]]   += 1
        for cat in b.get("keyword_tags", []):
            if cat: cat_c[cat] += 1

    def sp(d, top=0):
        pairs = sorted(d.items(), key=lambda x: -x[1])
        return pairs[:top] if top else pairs

    return {
        "by_status":   {"labels": [p[0] for p in sp(status_c)],    "data": [p[1] for p in sp(status_c)]},
        "by_state":    {"labels": [p[0] for p in sp(state_c, 15)], "data": [p[1] for p in sp(state_c, 15)]},
        "by_category": {"labels": [p[0] for p in sp(cat_c)],       "data": [p[1] for p in sp(cat_c)]},
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("Syncing data from Airtable…")
    bill_records       = fetch_table("Insurance Bills")
    legislator_records = fetch_table("Legislators")
    hearing_records    = fetch_table("Hearings")

    # Bills first — we need the bill_id→state lookup before processing legislators
    bills = transform_bills(bill_records)
    bill_state_lookup = {b["bill_id"]: b["state"] for b in bills if b["state"]}

    legislators = transform_legislators(legislator_records, bill_state_lookup)
    hearings    = transform_hearings(hearing_records)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary":      compute_summary(bills),
        "charts":       compute_charts(bills),
        "bills":        bills,
        "hearings":     hearings,
        "legislators":  legislators,
    }

    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, default=str)

    s = output["summary"]
    print(f"✓ data.json — {s['total']} bills | {s['states']} states | "
          f"{s['enacted']} enacted | {s['active']} active | {s['dead']} dead")


if __name__ == "__main__":
    main()
