#!/usr/bin/env python3
"""
Airtable → data.json sync script for the Insurance Bill Tracker.

Environment variables required (set as GitHub Actions secrets):
  AIRTABLE_KEY      — your Airtable Personal Access Token
  AIRTABLE_BASE_ID  — the base ID (e.g. appMLqvmasfgRiqrm)

Field-name assumptions (edit the FIELD_MAP dicts below if your columns differ):
  Insurance Bills table: Bill Title, Bill Number, Bill Progress, Year, Category,
                         Notes, Last Action Date, BT50 URL, Name, Summary,
                         Sponsor Names, Bill Last Action, Document Link,
                         Bill Added Date, Number of Votes
  Legislators table:     Name, Party, Role, District, State, In Office,
                         Ballotpedia URL, Follow the Money URL
  Hearings table:        Title, Start Date, End Date, Status, Location,
                         Description, Event Link, State, Bill Number, Bill Title
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
AIRTABLE_KEY = os.environ.get("AIRTABLE_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")

if not AIRTABLE_KEY or not AIRTABLE_BASE_ID:
    print("ERROR: AIRTABLE_KEY and AIRTABLE_BASE_ID must be set.", file=sys.stderr)
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {AIRTABLE_KEY}"}
BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"


# ---------------------------------------------------------------------------
# Airtable fetching
# ---------------------------------------------------------------------------
def fetch_table(table_name: str) -> list[dict]:
    """Fetch every record from a table, following pagination."""
    url = f"{BASE_URL}/{requests.utils.quote(table_name)}"
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

    print(f"  {table_name}: fetched {len(records)} records")
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fget(fields: dict, *keys, default=None):
    """Return the first matching key's value from fields, else default."""
    for k in keys:
        if k in fields:
            return fields[k]
    return default


def to_list(val) -> list:
    """Normalise a value to a list (handles None, string, list)."""
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def extract_state(title: str) -> str:
    """Pull the 2-letter state code from a title like 'CA SB123 (2025)'."""
    m = re.match(r"^([A-Z]{2})\s", title or "")
    return m.group(1) if m else ""


def status_bucket(status: str) -> str:
    """Classify a status string as 'enacted', 'dead', or 'active'."""
    s = (status or "").lower()
    if any(x in s for x in ("sign", "enact", "adopt", "passed")):
        return "enacted"
    if any(x in s for x in ("dead", "fail", "veto", "indef", "strip")):
        return "dead"
    return "active"


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
        year = [str(y) for y in year_raw if y]

        # Build a human-readable session string from year values
        years_sorted = sorted(year)
        if len(years_sorted) >= 2:
            session = f"{years_sorted[0]}-{years_sorted[-1]} Regular Session"
        elif years_sorted:
            session = f"{years_sorted[0]} Session"
        else:
            session = ""

        # Category — multi-select returns list of strings; linked records
        # return record IDs. We only keep strings here; if you use linked
        # records for Category, see the note in fetch_categories() below.
        cats_raw = to_list(fget(f, "Category"))
        keyword_tags = [c for c in cats_raw if isinstance(c, str)]

        raw_status = fget(f, "Bill Progress", default="")

        bills.append(
            {
                "bill_id": rec["id"],
                "state": state,
                "bill_number": fget(f, "Bill Number", default=""),
                "title": title,
                "status": raw_status,
                "status_bucket": status_bucket(raw_status),
                "session": session,
                "introduced_date": fget(f, "Bill Added Date", default=""),
                "last_action_date": fget(f, "Last Action Date", default=""),
                "last_action": fget(f, "Bill Last Action", default=""),
                "bill_url": fget(f, "BT50 URL", default=""),
                "document_link": fget(f, "Document Link", default=""),
                "keyword_tags": keyword_tags,
                "summary": fget(f, "Summary", default=""),
                "notes": fget(f, "Notes", default=""),
                "year": year,
                "session_label": session,
                # Use Sponsor Names if available, fall back to Name field
                "sponsors": fget(f, "Sponsor Names", "Name", default=""),
                "number_of_votes": fget(f, "Number of Votes", default=0),
            }
        )
    return bills


def transform_legislators(records: list[dict]) -> list[dict]:
    """
    Airtable field names assumed:
      Name, Party, Role, District, State, In Office,
      Ballotpedia URL, Follow the Money URL
    Adjust fget() calls below if yours differ.
    """
    legislators = []
    for rec in records:
        f = rec.get("fields", {})
        legislators.append(
            {
                "legislator_name": fget(f, "Name", "Legislator Name", default=""),
                "legislator_party": fget(f, "Party", default=""),
                "role": fget(f, "Role", "Title", "Position", default=""),
                "district": fget(f, "District", default=""),
                "state_code": fget(f, "State", "State Code", default=""),
                "in_office": fget(f, "In Office", default=True),
                "ballotpedia_url": fget(f, "Ballotpedia URL", "Ballotpedia", default=""),
                "follow_the_money_url": fget(
                    f, "Follow the Money URL", "Follow The Money", default=""
                ),
                "bill_count": fget(f, "Bill Count", "Bills", default=0),
                "states_active": to_list(fget(f, "States Active", "States")),
            }
        )
    return legislators


def transform_hearings(records: list[dict]) -> list[dict]:
    """
    Airtable field names assumed:
      Title, Start Date, End Date, Status, Location,
      Description, Event Link, State, Bill Number, Bill Title
    Adjust fget() calls below if yours differ.
    """
    hearings = []
    for rec in records:
        f = rec.get("fields", {})
        hearings.append(
            {
                "bill_id": fget(f, "Bill ID", default=""),
                "title": fget(f, "Title", "Name", "Hearing Title", default=""),
                "start_dt": fget(f, "Start Date", "Start", "Date", default=""),
                "end_dt": fget(f, "End Date", "End", default=""),
                "status": fget(f, "Status", default=""),
                "location": fget(f, "Location", "Committee", default=""),
                "description": fget(f, "Description", default=""),
                "event_link": fget(f, "Event Link", "URL", "Link", default=""),
                "state": fget(f, "State", default=""),
                "bill_number": fget(f, "Bill Number", default=""),
                "bill_title": fget(f, "Bill Title", default=""),
            }
        )
    return hearings


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def compute_summary(bills: list[dict]) -> dict:
    total = len(bills)
    states = len({b["state"] for b in bills if b["state"]})
    enacted = sum(1 for b in bills if b["status_bucket"] == "enacted")
    dead = sum(1 for b in bills if b["status_bucket"] == "dead")
    active = total - enacted - dead
    with_text = sum(1 for b in bills if b.get("summary") or b.get("document_link"))
    return {
        "total": total,
        "states": states,
        "enacted": enacted,
        "active": active,
        "dead": dead,
        "with_text": with_text,
    }


def compute_charts(bills: list[dict]) -> dict:
    status_counts: dict[str, int] = defaultdict(int)
    state_counts: dict[str, int] = defaultdict(int)
    cat_counts: dict[str, int] = defaultdict(int)

    for b in bills:
        if b["status"]:
            status_counts[b["status"]] += 1
        if b["state"]:
            state_counts[b["state"]] += 1
        for cat in b.get("keyword_tags", []):
            if cat:
                cat_counts[cat] += 1

    def sorted_pairs(d: dict, top: int = 0) -> list[tuple]:
        pairs = sorted(d.items(), key=lambda x: -x[1])
        return pairs[:top] if top else pairs

    status_pairs = sorted_pairs(status_counts)
    state_pairs = sorted_pairs(state_counts, top=15)
    cat_pairs = sorted_pairs(cat_counts)

    return {
        "by_status": {
            "labels": [p[0] for p in status_pairs],
            "data": [p[1] for p in status_pairs],
        },
        "by_state": {
            "labels": [p[0] for p in state_pairs],
            "data": [p[1] for p in state_pairs],
        },
        "by_category": {
            "labels": [p[0] for p in cat_pairs],
            "data": [p[1] for p in cat_pairs],
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("Syncing data from Airtable...")
    bill_records = fetch_table("Insurance Bills")
    legislator_records = fetch_table("Legislators")
    hearing_records = fetch_table("Hearings")

    bills = transform_bills(bill_records)
    legislators = transform_legislators(legislator_records)
    hearings = transform_hearings(hearing_records)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": compute_summary(bills),
        "charts": compute_charts(bills),
        "bills": bills,
        "hearings": hearings,
        "legislators": legislators,
    }

    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, default=str)

    s = output["summary"]
    print(
        f"✓ data.json written — "
        f"{s['total']} bills | {s['states']} states | "
        f"{s['enacted']} enacted | {s['active']} active | {s['dead']} dead"
    )


if __name__ == "__main__":
    main()
