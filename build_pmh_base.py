#!/usr/bin/env python3
"""
PMH Airtable Base Builder
Reads pmh-sold-projects.csv and pmh-collection-opportunities.csv and creates
a fully structured Airtable base with 8 tables: Customers, Team Members,
Projects, Change Orders, 3D Designs, Store Sales, Service Revenue,
Collection Opportunities.

Team Members are linked (not duplicated text) for Salesperson / Project Manager.
Change Orders and 3D Designs link to Projects; redundant fields (Salesperson,
Lead Source, PM, Sold Month) are omitted — create lookups in the Airtable UI.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, date as dtdate

import requests

# ── Config ──────────────────────────────────────────────────────────────────
API_TOKEN     = os.environ.get("AIRTABLE_TOKEN", "")
WORKSPACE     = "wspAHnobe45CQSA5S"
BASE_URL      = "https://api.airtable.com/v0"
CSV_FILE      = "pmh-sold-projects.csv"
COLL_CSV_FILE = "pmh-collection-opportunities.csv"
BASE_NAME     = "PMH Sold Projects"
BATCH_SIZE    = 10
RATE_DELAY    = 0.22

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type":  "application/json",
}

# ── Classification constants ────────────────────────────────────────────────
STORE_CUSTOMERS   = {"ESR Sales", "MSR Sales", "TSR Sales"}
SERVICE_CUSTOMERS = {"Service Revenue", "service Revenue", "Valet Revenue"}

STATUS_CHOICES = ["Active", "In Progress", "Paid/Closed", "Complete",
                  "On Hold", "Warranty"]


# ── Helpers ─────────────────────────────────────────────────────────────────
def api(method, url, data=None, retries=4):
    """Rate-limited API call with retry + exponential backoff."""
    time.sleep(RATE_DELAY)
    for attempt in range(retries):
        try:
            resp = getattr(requests, method.lower())(url, headers=HEADERS, json=data)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                print(f"    ⏳ rate-limited, waiting {wait}s …")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                print(f"    ✗ {resp.status_code}: {resp.text[:300]}")
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    return None


def classify(row):
    customer = row["Customer"].strip()
    ptype    = row["Project type"].strip()
    if customer in STORE_CUSTOMERS:
        return "store_sales"
    if customer in SERVICE_CUSTOMERS:
        return "service_revenue"
    if "Change Order" in ptype:
        return "change_order"
    if "3D Design" in ptype:
        return "three_d"
    return "project"


def base_customer_name(raw):
    """Strip project-type suffixes (C/O, HT, 3D, Swim Spa) to get the customer name."""
    name = raw.strip()
    for pat in [r"\s+C/O\s*(\(.*\))?\s*$",
                r"\s+HT\s*(\(.*\))?\s*$",
                r"\s+3D\s*(\(.*\))?\s*$",
                r"\s+Swim\s+Spa\s*$"]:
        cleaned = re.sub(pat, "", name)
        if cleaned != name:
            return cleaned.strip()
    return name


def strip_emoji(text):
    """Strip leading emoji/symbol chars: '🌳 Backyard Project' → 'Backyard Project'."""
    return re.sub(r"^[^\w]+\s*", "", text).strip()


def make_name(customer, ptype, sold_date_str):
    """Build primary field: 'Customer - Type - YYYY-MM-DD'."""
    d = date(sold_date_str) or "unknown"
    return f"{customer} - {strip_emoji(ptype)} - {d}"


def money(s):
    if not s or not s.strip():
        return None
    v = s.strip().replace("$", "").replace(",", "")
    try:
        f = float(v)
        return f if f else None
    except ValueError:
        return None


def date(s):
    if not s or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


_DOW = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_MON = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def parse_short_date(s):
    """Parse 'Thu Sep 11' → '2025-09-11' using day-of-week to resolve year."""
    s = s.strip()
    if not s:
        return None
    parts = s.split()
    if len(parts) != 3:
        return None
    dow, mon, day = _DOW.get(parts[0]), _MON.get(parts[1]), None
    try:
        day = int(parts[2])
    except ValueError:
        return None
    if dow is None or mon is None:
        return None
    for year in (2025, 2026):
        try:
            d = dtdate(year, mon, day)
            if d.weekday() == dow:
                return d.isoformat()
        except ValueError:
            continue
    return None


def batch_create(base_id, table_id, records):
    """Create records in batches of 10, return list of created records."""
    created = []
    total = len(records)
    for i in range(0, total, BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        payload = {"records": [{"fields": r} for r in batch]}
        result = api("post", f"{BASE_URL}/{base_id}/{table_id}", payload)
        if result and "records" in result:
            created.extend(result["records"])
        print(f"    {min(i + BATCH_SIZE, total)}/{total}", end="\r")
    print(f"    ✓ {len(created)}/{total} created")
    return created


def fetch_all(base_id, table_id, field_name):
    """Fetch all records from a table, returning only the named field."""
    records = []
    url = f"{BASE_URL}/{base_id}/{table_id}?pageSize=100&fields[]={field_name}"
    while url:
        data = api("get", url)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        url = (f"{BASE_URL}/{base_id}/{table_id}?pageSize=100"
               f"&fields[]={field_name}&offset={offset}") if offset else None
    return records


# ── Phase 1: Read & classify CSVs ──────────────────────────────────────────
def read_csv():
    cats = {k: [] for k in ("project", "change_order", "three_d",
                             "store_sales", "service_revenue")}
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            cats[classify(row)].append(row)
    return cats


def read_collection_csv():
    rows = []
    try:
        with open(COLL_CSV_FILE) as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except FileNotFoundError:
        print(f"    ⚠ {COLL_CSV_FILE} not found, skipping collection opportunities")
    return rows


# ── Phase 2: Collect unique field values ────────────────────────────────────
def collect_choices(cats):
    project_types = set()
    payment_methods = set()
    service_types = set()

    for row in cats["project"]:
        pt = row["Project type"].strip()
        if pt:
            project_types.add(pt)
        pm = row["Payment method"].strip()
        if pm:
            payment_methods.add(pm)

    for row in cats["change_order"]:
        pm = row["Payment method"].strip()
        if pm:
            payment_methods.add(pm)

    for row in cats["three_d"]:
        pm = row["Payment method"].strip()
        if pm:
            payment_methods.add(pm)

    for row in cats["service_revenue"]:
        st = row["Project type"].strip()
        if st:
            service_types.add(st)

    return {
        "project_types":   sorted(project_types),
        "payment_methods": sorted(payment_methods),
        "service_types":   sorted(service_types),
    }


def collect_collection_choices(coll_rows):
    phases = set()
    pay_methods = set()
    for row in coll_rows:
        pd = row["Payment description"].strip()
        if pd:
            phases.add(pd)
        pt = row["Payment type"].strip()
        if pt:
            pay_methods.add(pt)
    return {"coll_phases": sorted(phases), "coll_pay_methods": sorted(pay_methods)}


def collect_team_members(cats, coll_rows):
    """Collect all unique people names across all CSVs."""
    people = set()
    for cat in ("project", "change_order", "three_d", "service_revenue"):
        for row in cats[cat]:
            s = row["Salesperson"].strip()
            if s:
                people.add(s)
    for cat in ("project", "change_order"):
        for row in cats[cat]:
            m = row["Project manager"].strip()
            if m:
                people.add(m)
    for row in coll_rows:
        m = row["Project manager"].strip()
        if m:
            people.add(m)
    return sorted(people)


# ── Phase 3: Create Airtable base ──────────────────────────────────────────
def create_base(choices, coll_choices):
    def sel(names):
        return [{"name": n} for n in names]

    payload = {
        "name": BASE_NAME,
        "workspaceId": WORKSPACE,
        "tables": [
            # ── Team Members ──
            {
                "name": "Team Members",
                "fields": [
                    {"name": "Name", "type": "singleLineText"},
                ],
            },
            # ── Customers ──
            {
                "name": "Customers",
                "fields": [
                    {"name": "Name",       "type": "singleLineText"},
                    {"name": "Lead Source", "type": "singleLineText"},
                    {"name": "Notes",       "type": "multilineText"},
                ],
            },
            # ── Projects ──
            {
                "name": "Projects",
                "fields": [
                    {"name": "Project Name",    "type": "singleLineText"},
                    {"name": "Sold Date",       "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Contract Amount", "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Project Type",    "type": "singleSelect",
                     "options": {"choices": sel(choices["project_types"])}},
                    {"name": "Payment Method",  "type": "singleSelect",
                     "options": {"choices": sel(choices["payment_methods"])}},
                    {"name": "Lead Source",     "type": "singleLineText"},
                    {"name": "WT Date",         "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Status", "type": "singleSelect",
                     "options": {"choices": sel(STATUS_CHOICES)}},
                ],
            },
            # ── Change Orders (Salesperson/Lead Source/PM/Sold Month
            #    are lookups from Project — create in UI) ──
            {
                "name": "Change Orders",
                "fields": [
                    {"name": "Change Order",   "type": "singleLineText"},
                    {"name": "Sold Date",      "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Amount",         "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Payment Method", "type": "singleSelect",
                     "options": {"choices": sel(choices["payment_methods"])}},
                ],
            },
            # ── 3D Designs (Salesperson/Lead Source/Sold Month
            #    are lookups from Project — create in UI) ──
            {
                "name": "3D Designs",
                "fields": [
                    {"name": "Design Name",    "type": "singleLineText"},
                    {"name": "Sold Date",      "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Fee",            "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Payment Method", "type": "singleSelect",
                     "options": {"choices": sel(choices["payment_methods"])}},
                ],
            },
            # ── Store Sales ──
            {
                "name": "Store Sales",
                "fields": [
                    {"name": "Sale ID",    "type": "singleLineText"},
                    {"name": "Store",      "type": "singleSelect",
                     "options": {"choices": sel(["ESR Sales", "MSR Sales", "TSR Sales"])}},
                    {"name": "Sale Date",  "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Amount",     "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Lead Source", "type": "singleLineText"},
                ],
            },
            # ── Service Revenue ──
            {
                "name": "Service Revenue",
                "fields": [
                    {"name": "Entry ID",     "type": "singleLineText"},
                    {"name": "Service Type", "type": "singleSelect",
                     "options": {"choices": sel(choices["service_types"])}},
                    {"name": "Date",         "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Amount",       "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Lead Source",  "type": "singleLineText"},
                ],
            },
            # ── Collection Opportunities ──
            {
                "name": "Collection Opportunities",
                "fields": [
                    {"name": "Collection ID",     "type": "singleLineText"},
                    {"name": "Payment Phase",     "type": "singleSelect",
                     "options": {"choices": sel(coll_choices["coll_phases"])}},
                    {"name": "Collection Amount",  "type": "currency",
                     "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Original Due Date",  "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Updated Due Date",   "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Date Collected",     "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Date Verified",      "type": "date",
                     "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Status", "type": "singleSelect",
                     "options": {"choices": sel(["Not paid", "✅ Paid", "Verified"])}},
                    {"name": "Payment Method", "type": "singleSelect",
                     "options": {"choices": sel(coll_choices["coll_pay_methods"])}},
                    {"name": "Notes",              "type": "multilineText"},
                ],
            },
        ],
    }

    print("  Creating base with 8 tables …")
    result = api("post", f"{BASE_URL}/meta/bases", payload)
    base_id = result["id"]
    tables = {}
    for t in result["tables"]:
        tables[t["name"]] = {
            "id": t["id"],
            "fields": {f["name"]: f["id"] for f in t["fields"]},
        }
    print(f"  ✓ Base created: {base_id}")
    return base_id, tables


# ── Phase 4: Add linked-record fields ──────────────────────────────────────
def add_links(base_id, tables):
    tm_id   = tables["Team Members"]["id"]
    cust_id = tables["Customers"]["id"]
    proj_id = tables["Projects"]["id"]
    co_id   = tables["Change Orders"]["id"]

    def link(table_name, field_name, target_id):
        print(f"  Linking {table_name}.{field_name} …")
        result = api("post",
            f"{BASE_URL}/meta/bases/{base_id}/tables/{tables[table_name]['id']}/fields",
            {"name": field_name, "type": "multipleRecordLinks",
             "options": {"linkedTableId": target_id}})
        tables[table_name]["fields"][field_name] = result["id"]

    # Customers → Team Members
    link("Customers", "Primary Salesperson", tm_id)

    # Projects → Customers
    link("Projects", "Customer", cust_id)
    # Projects → Team Members (Salesperson + Project Manager)
    link("Projects", "Salesperson", tm_id)
    link("Projects", "Project Manager", tm_id)

    # Change Orders → Projects
    link("Change Orders", "Project", proj_id)

    # 3D Designs → Projects
    link("3D Designs", "Project", proj_id)

    # Service Revenue → Team Members
    link("Service Revenue", "Salesperson", tm_id)

    # Collection Opportunities → Projects + Change Orders + Team Members
    link("Collection Opportunities", "Project", proj_id)
    link("Collection Opportunities", "Change Order", co_id)
    link("Collection Opportunities", "Project Manager", tm_id)

    print("  ✓ Links added")
    return tables


# ── Phase 5: Populate records ───────────────────────────────────────────────
def populate_team_members(base_id, tables, people):
    """Create team member records, return name→ID map."""
    records = [{"Name": name} for name in people]
    print(f"  Team Members ({len(records)}) …")
    created = batch_create(base_id, tables["Team Members"]["id"], records)
    return {r["fields"]["Name"]: r["id"] for r in created}


def populate_customers(base_id, tables, cats, tm_map):
    """Deduplicate customers, return name→ID map."""
    info = {}
    for cat in ("project", "change_order", "three_d"):
        for row in cats[cat]:
            name = base_customer_name(row["Customer"])
            if name not in info:
                info[name] = {
                    "lead_source":  row["Lead source"].strip(),
                    "salesperson":  row["Salesperson"].strip(),
                }

    records = []
    for name in sorted(info):
        rec = {"Name": name}
        if info[name]["lead_source"]:
            rec["Lead Source"] = info[name]["lead_source"]
        sp = info[name]["salesperson"]
        if sp and sp in tm_map:
            rec["Primary Salesperson"] = [tm_map[sp]]
        records.append(rec)

    print(f"  Customers ({len(records)}) …")
    created = batch_create(base_id, tables["Customers"]["id"], records)
    return {r["fields"]["Name"]: r["id"] for r in created}


def populate_projects(base_id, tables, cats, cmap, tm_map):
    records = []
    for row in cats["project"]:
        cname = base_customer_name(row["Customer"])
        rec = {
            "Project Name": make_name(cname, row["Project type"], row["Sold date"]),
            "Project Type": row["Project type"].strip(),
            "Lead Source":  row["Lead source"].strip(),
        }
        d = date(row["Sold date"]);        rec["Sold Date"]       = d if d else None
        a = money(row["Job amount"]);      rec["Contract Amount"] = a if a else None
        p = row["Payment method"].strip(); rec["Payment Method"]  = p if p else None
        w = date(row["WT date"]);          rec["WT Date"]         = w if w else None
        s = row["Status"].strip();         rec["Status"]          = s if s else None

        if cmap.get(cname):
            rec["Customer"] = [cmap[cname]]
        sp = row["Salesperson"].strip()
        if sp and sp in tm_map:
            rec["Salesperson"] = [tm_map[sp]]
        pm = row["Project manager"].strip()
        if pm and pm in tm_map:
            rec["Project Manager"] = [tm_map[pm]]

        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Projects ({len(records)}) …")
    created = batch_create(base_id, tables["Projects"]["id"], records)

    # Build customer_name → best project ID map
    TYPE_PRIORITY = ["Pool Project", "Backyard Project", "Swim Spa",
                     "Non Warranty Repair", "Hot Tub", "Valet Service"]
    cust_projects = {}
    for rec in created:
        f = rec["fields"]
        cust_ids = f.get("Customer", [])
        ptype = f.get("Project Type", "")
        for cid in cust_ids:
            for cname, rid in cmap.items():
                if rid == cid:
                    cust_projects.setdefault(cname, []).append((rec["id"], ptype))
                    break

    pmap = {}
    for cname, projs in cust_projects.items():
        if len(projs) == 1:
            pmap[cname] = projs[0][0]
        else:
            for keyword in TYPE_PRIORITY:
                for pid, pt in projs:
                    if keyword in pt:
                        pmap[cname] = pid
                        break
                if cname in pmap:
                    break
            if cname not in pmap:
                pmap[cname] = projs[0][0]

    return pmap


def populate_change_orders(base_id, tables, cats, pmap):
    records = []
    for row in cats["change_order"]:
        cname = base_customer_name(row["Customer"])
        rec = {
            "Change Order": make_name(cname, "Change Order", row["Sold date"]),
        }
        d = date(row["Sold date"]);        rec["Sold Date"]      = d
        a = money(row["Job amount"]);      rec["Amount"]         = a
        p = row["Payment method"].strip(); rec["Payment Method"] = p if p else None
        if pmap.get(cname):
            rec["Project"] = [pmap[cname]]
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Change Orders ({len(records)}) …")
    batch_create(base_id, tables["Change Orders"]["id"], records)


def populate_3d(base_id, tables, cats, pmap):
    records = []
    for row in cats["three_d"]:
        cname = base_customer_name(row["Customer"])
        rec = {
            "Design Name": make_name(cname, "3D Design", row["Sold date"]),
        }
        d = date(row["Sold date"]);        rec["Sold Date"]      = d
        a = money(row["Job amount"]);      rec["Fee"]            = a
        p = row["Payment method"].strip(); rec["Payment Method"] = p if p else None
        if pmap.get(cname):
            rec["Project"] = [pmap[cname]]
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  3D Designs ({len(records)}) …")
    batch_create(base_id, tables["3D Designs"]["id"], records)


def populate_store_sales(base_id, tables, cats):
    records = []
    for row in cats["store_sales"]:
        rec = {
            "Sale ID":    make_name(row["Customer"].strip(), "Showroom Sales", row["Sold date"]),
            "Store":      row["Customer"].strip(),
            "Lead Source": row["Lead source"].strip(),
        }
        d = date(row["Sold date"]); rec["Sale Date"] = d
        a = money(row["Job amount"]); rec["Amount"] = a
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Store Sales ({len(records)}) …")
    batch_create(base_id, tables["Store Sales"]["id"], records)


def populate_service_revenue(base_id, tables, cats, tm_map):
    records = []
    for row in cats["service_revenue"]:
        rec = {
            "Entry ID":     make_name(row["Customer"].strip(), row["Project type"], row["Sold date"]),
            "Service Type": row["Project type"].strip(),
            "Lead Source":  row["Lead source"].strip(),
        }
        d = date(row["Sold date"]); rec["Date"] = d
        a = money(row["Job amount"]); rec["Amount"] = a
        sp = row["Salesperson"].strip()
        if sp and sp in tm_map:
            rec["Salesperson"] = [tm_map[sp]]
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Service Revenue ({len(records)}) …")
    batch_create(base_id, tables["Service Revenue"]["id"], records)


def populate_collections(base_id, tables, coll_rows, proj_name_map,
                         co_name_map, tm_map):
    """Populate Collection Opportunities, linking to Projects/COs/Team Members."""
    records = []
    linked_p = linked_co = 0

    for row in coll_rows:
        pid = row["Project ID"].strip()
        cname = row["Client name"].strip()
        phase = row["Payment description"].strip()
        phase_clean = strip_emoji(phase).strip()
        updated = parse_short_date(row["Updated due date"])

        primary = f"{cname} - {phase_clean}"
        if updated:
            primary += f" - {updated}"

        rec = {"Collection ID": primary}
        if phase:
            rec["Payment Phase"] = phase
        amt = money(row["Collection amount"])
        if amt is not None:
            rec["Collection Amount"] = amt

        orig = parse_short_date(row["Original due date"])
        if orig:
            rec["Original Due Date"] = orig
        if updated:
            rec["Updated Due Date"] = updated
        coll = parse_short_date(row["Date collected"])
        if coll:
            rec["Date Collected"] = coll
        ver = parse_short_date(row["Date verified"])
        if ver:
            rec["Date Verified"] = ver

        status = row["Status"].strip()
        if status:
            rec["Status"] = status
        pmethod = row["Payment type"].strip()
        if pmethod:
            rec["Payment Method"] = pmethod
        pm = row["Project manager"].strip()
        if pm and pm in tm_map:
            rec["Project Manager"] = [tm_map[pm]]
        notes = row["Notes"].strip()
        if notes:
            rec["Notes"] = notes

        npid = _normalize_collection_pid(pid)
        if "Change Order" in pid:
            rid = co_name_map.get(npid)
            if rid:
                rec["Change Order"] = [rid]
                linked_co += 1
        else:
            rid = proj_name_map.get(npid)
            if rid:
                rec["Project"] = [rid]
                linked_p += 1

        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Collection Opportunities ({len(records)}) "
          f"— {linked_p} proj, {linked_co} CO linked …")
    batch_create(base_id, tables["Collection Opportunities"]["id"], records)


def _normalize_collection_pid(pid):
    """Normalize a Collection Opportunities Project ID for matching."""
    pid = pid.strip()
    m = re.match(r"^(.+) - (\d{2})-(\d{2})-(\d{4})$", pid)
    if m:
        prefix, mm, dd, yyyy = m.groups()
        pid = f"{prefix} - {yyyy}-{mm}-{dd}"
    pid = re.sub(r"\s+C/O(\s|$)", r"\1", pid).strip()
    return re.sub(r"\s+", " ", strip_emoji(pid)).strip()


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    if not API_TOKEN:
        print("ERROR: Set AIRTABLE_TOKEN environment variable first.")
        print("  export AIRTABLE_TOKEN='pat...'")
        sys.exit(1)

    print("=" * 60)
    print("PMH Airtable Base Builder")
    print("=" * 60)

    # Phase 1 — Read CSVs
    print("\n▸ Reading CSVs …")
    cats = read_csv()
    for cat, rows in cats.items():
        print(f"    {cat:20s} {len(rows):>4} rows")
    coll_rows = read_collection_csv()
    print(f"    {'collection':20s} {len(coll_rows):>4} rows")

    # Phase 2 — Collect choices
    print("\n▸ Collecting field choices …")
    choices = collect_choices(cats)
    for k, v in choices.items():
        print(f"    {k}: {v}")
    coll_choices = collect_collection_choices(coll_rows)
    print(f"    coll_phases: {len(coll_choices['coll_phases'])} values")
    print(f"    coll_pay_methods: {coll_choices['coll_pay_methods']}")
    people = collect_team_members(cats, coll_rows)
    print(f"    team_members: {len(people)} people")

    # Phase 3 — Create base
    print("\n▸ Creating Airtable base …")
    base_id, tables = create_base(choices, coll_choices)

    # Phase 4 — Add links
    print("\n▸ Adding linked-record fields …")
    tables = add_links(base_id, tables)

    # Phase 5 — Populate
    print("\n▸ Populating records …")
    tm_map = populate_team_members(base_id, tables, people)
    cmap = populate_customers(base_id, tables, cats, tm_map)
    print(f"    ({len(cmap)} unique customers)")
    pmap = populate_projects(base_id, tables, cats, cmap, tm_map)
    print(f"    ({len(pmap)} customers with project links)")
    populate_change_orders(base_id, tables, cats, pmap)
    populate_3d(base_id, tables, cats, pmap)
    populate_store_sales(base_id, tables, cats)
    populate_service_revenue(base_id, tables, cats, tm_map)

    # Phase 6 — Collection Opportunities (needs name→ID maps from created records)
    if coll_rows:
        print("\n▸ Building name maps for collections …")
        proj_recs = fetch_all(base_id, tables["Projects"]["id"], "Project Name")
        proj_name_map = {}
        for r in proj_recs:
            name = r["fields"].get("Project Name", "")
            key = re.sub(r"\s+", " ", strip_emoji(name)).strip()
            proj_name_map[key] = r["id"]
        print(f"    {len(proj_name_map)} projects indexed")

        co_recs = fetch_all(base_id, tables["Change Orders"]["id"], "Change Order")
        co_name_map = {}
        for r in co_recs:
            name = r["fields"].get("Change Order", "")
            key = re.sub(r"\s+", " ", strip_emoji(name)).strip()
            co_name_map[key] = r["id"]
        print(f"    {len(co_name_map)} change orders indexed")

        print("\n▸ Populating Collection Opportunities …")
        populate_collections(base_id, tables, coll_rows,
                             proj_name_map, co_name_map, tm_map)

    # Done
    print("\n" + "=" * 60)
    print(f"✓ Base created: {base_id}")
    print(f"  https://airtable.com/{base_id}")
    print()
    print("Manual steps needed in Airtable UI:")
    print("  1. Convert primary fields to Formula type (see README)")
    print("  2. Create Sold Month formula: DATETIME_FORMAT({Sold Date}, 'MMMM YYYY')")
    print("  3. Add lookup fields on Change Orders (Salesperson, Lead Source, PM from Project)")
    print("  4. Add lookup fields on 3D Designs (Salesperson, Lead Source from Project)")
    print("  5. Add rollup fields on Projects for collection totals")
    print("  6. Delete any '- delete' fields from prior migrations")
    print("=" * 60)


if __name__ == "__main__":
    main()
