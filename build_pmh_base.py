#!/usr/bin/env python3
"""
PMH Airtable Base Builder
Reads pmh-sold-projects.csv and creates a fully structured Airtable base
with 6 tables: Customers, Projects, Change Orders, 3D Designs, Store Sales, Service Revenue.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

# ── Config ──────────────────────────────────────────────────────────────────
API_TOKEN  = os.environ.get("AIRTABLE_TOKEN", "")
WORKSPACE  = "wspAHnobe45CQSA5S"
BASE_URL   = "https://api.airtable.com/v0"
CSV_FILE   = "pmh-sold-projects.csv"
BASE_NAME  = "PMH Sold Projects"
BATCH_SIZE = 10          # Airtable max per request
RATE_DELAY = 0.22        # stay under 5 req/s

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type":  "application/json",
}

# ── Classification constants ────────────────────────────────────────────────
STORE_CUSTOMERS   = {"ESR Sales", "MSR Sales", "TSR Sales"}
SERVICE_CUSTOMERS = {"Service Revenue", "service Revenue", "Valet Revenue"}


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


# ── Phase 1: Read & classify CSV ───────────────────────────────────────────
def read_csv():
    cats = {k: [] for k in ("project", "change_order", "three_d", "store_sales", "service_revenue")}
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            cats[classify(row)].append(row)
    return cats


# ── Phase 2: Collect unique select-field values ─────────────────────────────
def collect_choices(cats):
    """Scan data to build select-field choice lists."""
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


# ── Phase 3: Create Airtable base ──────────────────────────────────────────
def create_base(choices):
    def sel(names):
        return [{"name": n} for n in names]

    payload = {
        "name": BASE_NAME,
        "workspaceId": WORKSPACE,
        "tables": [
            # ── Customers ──
            {
                "name": "Customers",
                "fields": [
                    {"name": "Name",               "type": "singleLineText"},
                    {"name": "Lead Source",         "type": "singleLineText"},
                    {"name": "Primary Salesperson", "type": "singleLineText"},
                    {"name": "Notes",               "type": "multilineText"},
                ],
            },
            # ── Projects ──
            {
                "name": "Projects",
                "fields": [
                    {"name": "Project Name",    "type": "singleLineText"},
                    {"name": "Sold Date",       "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Salesperson",     "type": "singleLineText"},
                    {"name": "Contract Amount", "type": "currency", "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Project Type",    "type": "singleSelect", "options": {"choices": sel(choices["project_types"])}},
                    {"name": "Payment Method",  "type": "singleSelect", "options": {"choices": sel(choices["payment_methods"])}},
                    {"name": "Lead Source",     "type": "singleLineText"},
                    {"name": "Project Manager", "type": "singleLineText"},
                    {"name": "WT Date",         "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Status",          "type": "singleLineText"},
                    {"name": "Sold Month",      "type": "singleLineText"},
                ],
            },
            # ── Change Orders ──
            {
                "name": "Change Orders",
                "fields": [
                    {"name": "Change Order",    "type": "singleLineText"},
                    {"name": "Sold Date",       "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Salesperson",     "type": "singleLineText"},
                    {"name": "Amount",          "type": "currency", "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Payment Method",  "type": "singleSelect", "options": {"choices": sel(choices["payment_methods"])}},
                    {"name": "Lead Source",     "type": "singleLineText"},
                    {"name": "Project Manager", "type": "singleLineText"},
                    {"name": "Sold Month",      "type": "singleLineText"},
                ],
            },
            # ── 3D Designs ──
            {
                "name": "3D Designs",
                "fields": [
                    {"name": "Design Name",    "type": "singleLineText"},
                    {"name": "Sold Date",      "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Salesperson",    "type": "singleLineText"},
                    {"name": "Fee",            "type": "currency", "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Payment Method", "type": "singleSelect", "options": {"choices": sel(choices["payment_methods"])}},
                    {"name": "Lead Source",    "type": "singleLineText"},
                    {"name": "Sold Month",     "type": "singleLineText"},
                ],
            },
            # ── Store Sales ──
            {
                "name": "Store Sales",
                "fields": [
                    {"name": "Sale ID",    "type": "singleLineText"},
                    {"name": "Store",      "type": "singleSelect", "options": {"choices": sel(["ESR Sales", "MSR Sales", "TSR Sales"])}},
                    {"name": "Sale Date",  "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Amount",     "type": "currency", "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Lead Source", "type": "singleLineText"},
                    {"name": "Sold Month", "type": "singleLineText"},
                ],
            },
            # ── Service Revenue ──
            {
                "name": "Service Revenue",
                "fields": [
                    {"name": "Entry ID",      "type": "singleLineText"},
                    {"name": "Service Type",  "type": "singleSelect", "options": {"choices": sel(choices["service_types"])}},
                    {"name": "Date",          "type": "date", "options": {"dateFormat": {"name": "us"}}},
                    {"name": "Amount",        "type": "currency", "options": {"precision": 2, "symbol": "$"}},
                    {"name": "Salesperson",   "type": "singleLineText"},
                    {"name": "Lead Source",   "type": "singleLineText"},
                    {"name": "Sold Month",    "type": "singleLineText"},
                ],
            },
        ],
    }

    print("  Creating base with 6 tables …")
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
    cust_id = tables["Customers"]["id"]
    proj_id = tables["Projects"]["id"]

    # Projects → Customers
    print("  Linking Projects → Customers …")
    result = api("post",
        f"{BASE_URL}/meta/bases/{base_id}/tables/{proj_id}/fields",
        {"name": "Customer", "type": "multipleRecordLinks",
         "options": {"linkedTableId": cust_id}})
    tables["Projects"]["fields"]["Customer"] = result["id"]

    # Change Orders → Projects
    print("  Linking Change Orders → Projects …")
    result = api("post",
        f"{BASE_URL}/meta/bases/{base_id}/tables/{tables['Change Orders']['id']}/fields",
        {"name": "Project", "type": "multipleRecordLinks",
         "options": {"linkedTableId": proj_id}})
    tables["Change Orders"]["fields"]["Project"] = result["id"]

    # 3D Designs → Projects
    print("  Linking 3D Designs → Projects …")
    result = api("post",
        f"{BASE_URL}/meta/bases/{base_id}/tables/{tables['3D Designs']['id']}/fields",
        {"name": "Project", "type": "multipleRecordLinks",
         "options": {"linkedTableId": proj_id}})
    tables["3D Designs"]["fields"]["Project"] = result["id"]

    print("  ✓ Links added")
    return tables


# ── Phase 5: Populate records ───────────────────────────────────────────────
def populate_customers(base_id, tables, cats):
    """Deduplicate customers from projects + COs + 3D designs, return name→ID map."""
    info = {}  # name → {lead_source, salesperson}
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
        if info[name]["salesperson"]:
            rec["Primary Salesperson"] = info[name]["salesperson"]
        records.append(rec)

    print(f"  Customers ({len(records)}) …")
    created = batch_create(base_id, tables["Customers"]["id"], records)
    return {r["fields"]["Name"]: r["id"] for r in created}


def populate_projects(base_id, tables, cats, cmap):
    records = []
    for row in cats["project"]:
        cname = base_customer_name(row["Customer"])
        rec = {
            "Project Name": make_name(cname, row["Project type"], row["Sold date"]),
            "Salesperson":  row["Salesperson"].strip(),
            "Project Type": row["Project type"].strip(),
            "Lead Source":  row["Lead source"].strip(),
            "Sold Month":   row["Sold month"].strip(),
        }
        d = date(row["Sold date"]);       rec["Sold Date"]       = d if d else rec.pop("Sold Date", None)
        a = money(row["Job amount"]);     rec["Contract Amount"] = a if a else rec.pop("Contract Amount", None)
        p = row["Payment method"].strip(); rec["Payment Method"] = p if p else rec.pop("Payment Method", None)
        m = row["Project manager"].strip(); rec["Project Manager"] = m if m else rec.pop("Project Manager", None)
        w = date(row["WT date"]);          rec["WT Date"]        = w if w else rec.pop("WT Date", None)
        s = row["Status"].strip();         rec["Status"]         = s if s else rec.pop("Status", None)
        if cmap.get(cname):
            rec["Customer"] = [cmap[cname]]
        # Clean up None values
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Projects ({len(records)}) …")
    created = batch_create(base_id, tables["Projects"]["id"], records)

    # Build customer_name → best project ID map (prefer Pool > Backyard > Swim Spa > other)
    TYPE_PRIORITY = ["Pool Project", "Backyard Project", "Swim Spa",
                     "Non Warranty Repair", "Hot Tub", "Valet Service"]
    cust_projects = {}  # customer_name → [(record_id, project_type)]
    for rec in created:
        f = rec["fields"]
        cust_ids = f.get("Customer", [])
        ptype = f.get("Project Type", "")
        for cid in cust_ids:
            # reverse-lookup customer name from cmap
            for cname, rid in cmap.items():
                if rid == cid:
                    cust_projects.setdefault(cname, []).append((rec["id"], ptype))
                    break

    pmap = {}  # customer_name → best project record ID
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
            "Salesperson":     row["Salesperson"].strip(),
            "Lead Source":     row["Lead source"].strip(),
            "Sold Month":      row["Sold month"].strip(),
        }
        d = date(row["Sold date"]);        rec["Sold Date"]       = d
        a = money(row["Job amount"]);      rec["Amount"]          = a
        p = row["Payment method"].strip(); rec["Payment Method"]  = p if p else None
        m = row["Project manager"].strip(); rec["Project Manager"] = m if m else None
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
            "Salesperson":  row["Salesperson"].strip(),
            "Lead Source":  row["Lead source"].strip(),
            "Sold Month":   row["Sold month"].strip(),
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
            "Sale ID": make_name(row["Customer"].strip(), "Showroom Sales", row["Sold date"]),
            "Store":      row["Customer"].strip(),
            "Lead Source": row["Lead source"].strip(),
            "Sold Month": row["Sold month"].strip(),
        }
        d = date(row["Sold date"]); rec["Sale Date"] = d
        a = money(row["Job amount"]); rec["Amount"] = a
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Store Sales ({len(records)}) …")
    batch_create(base_id, tables["Store Sales"]["id"], records)


def populate_service_revenue(base_id, tables, cats):
    records = []
    for row in cats["service_revenue"]:
        rec = {
            "Entry ID": make_name(row["Customer"].strip(), row["Project type"], row["Sold date"]),
            "Service Type": row["Project type"].strip(),
            "Salesperson":  row["Salesperson"].strip(),
            "Lead Source":  row["Lead source"].strip(),
            "Sold Month":   row["Sold month"].strip(),
        }
        d = date(row["Sold date"]); rec["Date"] = d
        a = money(row["Job amount"]); rec["Amount"] = a
        rec = {k: v for k, v in rec.items() if v is not None}
        records.append(rec)

    print(f"  Service Revenue ({len(records)}) …")
    batch_create(base_id, tables["Service Revenue"]["id"], records)


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    if not API_TOKEN:
        print("ERROR: Set AIRTABLE_TOKEN environment variable first.")
        print("  export AIRTABLE_TOKEN='pat...'")
        sys.exit(1)

    print("=" * 60)
    print("PMH Airtable Base Builder")
    print("=" * 60)

    # Phase 1
    print("\n▸ Reading CSV …")
    cats = read_csv()
    for cat, rows in cats.items():
        print(f"    {cat:20s} {len(rows):>4} rows")

    # Phase 2
    print("\n▸ Collecting field choices …")
    choices = collect_choices(cats)
    for k, v in choices.items():
        print(f"    {k}: {v}")

    # Phase 3
    print("\n▸ Creating Airtable base …")
    base_id, tables = create_base(choices)

    # Phase 4
    print("\n▸ Adding linked-record fields …")
    tables = add_links(base_id, tables)

    # Phase 5
    print("\n▸ Populating records …")
    cmap = populate_customers(base_id, tables, cats)
    print(f"    ({len(cmap)} unique customers)")
    pmap = populate_projects(base_id, tables, cats, cmap)
    print(f"    ({len(pmap)} customers with project links)")
    populate_change_orders(base_id, tables, cats, pmap)
    populate_3d(base_id, tables, cats, pmap)
    populate_store_sales(base_id, tables, cats)
    populate_service_revenue(base_id, tables, cats)

    # Done
    print("\n" + "=" * 60)
    print(f"✓ Base created: {base_id}")
    print(f"  https://airtable.com/{base_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
