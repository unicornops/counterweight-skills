#!/usr/bin/env python3
"""Normalise cloud billing exports into a Counterweight estate file (estate.json).

Supported inputs (CSV, .csv.gz, or .zip of CSVs; Parquet if pyarrow is installed):
  * AWS Cost and Usage Report, legacy (lineItem/...) and CUR 2.0 (line_item_...)
  * FOCUS exports from AWS, Azure, Google Cloud, Oracle (OCI), IBM Cloud and Alibaba Cloud
    (BilledCost, EffectiveCost, ServiceName ...); Alibaba's FOCUS preview carries no quantities
  * Azure Cost Management exports, actual or amortised (MeterCategory, CostInBillingCurrency ...)
  * Google Cloud billing export to BigQuery, saved as CSV (service.description, sku.description ...)
  * Oracle Cloud (OCI) cost reports (lineItem/..., product/service, cost/myCost ...)
  * IBM Cloud usage CSVs: the "Instances" export (Usage Metric, Usage Quantity, Cost ...) and the
    Account Resource Usage section of the "Summary" export
  * Any two-column service/cost summary (e.g. a Cost Explorer CSV): service-level only

Alibaba Cloud's bill-details CSVs are not parsed (their headers are not documented); use its FOCUS
export. PDF invoices are not parsed here. Read them yourself and write estate.json by hand
following references/estate-schema.md, or write a small CSV with columns
service,description,quantity,unit,cost and pass it with --format summary.

Usage:
  python3 ingest.py BILL.csv [MORE.csv ...] -o estate.json [--format auto|aws|azure|gcp|oci|ibm|focus|summary]
                    [--currency USD] [--period-hours 730]

Only the standard library is required. Prints a short summary to stderr and writes JSON.
Exit codes: 0 ok, 2 no usable rows, 3 unreadable input.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from specs import decode, find_instance_type, oci_series, oci_traits  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

SCHEMA = "counterweight.estate/1"

# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def norm_header(h: str) -> str:
    h = h.strip().lstrip("﻿")
    h = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", h)
    h = re.sub(r"[^A-Za-z0-9]+", "_", h).strip("_").lower()
    return h


def _open_text(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", newline="")
    return open(path, encoding="utf-8-sig", newline="")


def iter_rows(path: str):
    """Yield dict rows with normalised headers from csv / csv.gz / zip / parquet."""
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".csv", ".csv.gz")) and not name.startswith("__MACOSX"):
                    raw = z.read(name)
                    if name.lower().endswith(".gz"):
                        raw = gzip.decompress(raw)
                    yield from _iter_csv(io.StringIO(raw.decode("utf-8-sig")))
        return
    if low.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq  # optional
        except ImportError:
            sys.exit("Parquet input needs pyarrow, which is not installed. Export as CSV or run: "
                     "pip install pyarrow")
        table = pq.read_table(path)
        cols = [norm_header(c) for c in table.column_names]
        for batch in table.to_batches(50000):
            d = batch.to_pydict()
            keys = list(d.keys())
            for i in range(batch.num_rows):
                row = {}
                for k, nk in zip(keys, cols):
                    v = d[k][i]
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v if not isinstance(v, list) else dict(v) if v and isinstance(v[0], tuple) else v)
                    row[nk] = "" if v is None else str(v)
                yield row
        return
    with _open_text(path) as f:
        yield from _iter_csv(f)


def _iter_csv(f):
    sample = f.read(65536)
    f.seek(0) if hasattr(f, "seek") else None
    try:
        dialect = csv.Sniffer().sniff(sample.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(f, dialect)
    header = None
    meta_keys = pending = None
    FILE_META.clear()
    for rec in reader:
        if not rec or all(not c.strip() for c in rec):
            continue
        if header is None:
            if sum(1 for c in rec if c.strip()) < 2:
                continue  # a section title ("Account Metadata"), not a header
            norm = [norm_header(c) for c in rec]
            if "billing_month" in norm and ("account_owner_id" in norm or "entity_id" in norm):
                meta_keys = pending = norm  # IBM Cloud: a metadata block comes before the usage table
                continue
            if meta_keys is not None:
                if "service_name" in norm and ("usage_metric" in norm or "metric" in norm):
                    header = norm
                elif "support_cost" in norm:
                    pending = norm  # the summary export's Support Summary section: keep its first row
                elif pending is not None:
                    FILE_META.update({k: v.strip() for k, v in zip(pending, rec) if k not in FILE_META})
                    pending = None
                continue  # section titles and other sections before the usage table
            header = norm
            continue
        yield dict(zip(header, rec))


# Key/value metadata that some exports put above their table (IBM Cloud's Account Metadata block)
FILE_META = {}

# Set per input file by sniff_locale(): European exports use ";" with decimal commas and DD/MM dates.
LOCALE = {"decimal_comma": False, "dayfirst": False}


def sniff_locale(path: str, max_rows: int = 3000) -> dict:
    """Look at the first rows of a file and decide its decimal mark and date order."""
    comma_dec = dot_dec = 0
    first_gt12 = second_gt12 = 0
    n = 0
    for row in iter_rows(path):
        for v in row.values():
            v = (v or "").strip()
            if re.fullmatch(r"-?\d+,\d+", v):
                comma_dec += 1
            elif re.fullmatch(r"-?\d+\.\d+", v):
                dot_dec += 1
            m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})", v)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                first_gt12 += a > 12
                second_gt12 += b > 12
        n += 1
        if n >= max_rows:
            break
    loc = {"decimal_comma": comma_dec > dot_dec and comma_dec > 0,
           "dayfirst": first_gt12 > 0 and second_gt12 == 0,
           "date_order_ambiguous": first_gt12 == 0 and second_gt12 == 0 and n > 0}
    return loc


def fnum(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace("$", "").replace("€", "").replace("£", "").replace("\u00a0", "").replace(" ", "")
    if not s:
        return 0.0
    if LOCALE["decimal_comma"]:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def first(row: dict, *keys, default=""):
    for k in keys:
        if k in row and str(row[k]).strip() != "":
            return row[k]
    return default


def parse_date(s: str):
    """Parse the date/time formats seen in AWS, Azure and GCP exports. Returns naive UTC or None."""
    s = (s or "").strip().replace(" UTC", "").replace("Z", "")
    if not s:
        return None
    s = re.sub(r"([+-]\d{2}):?(\d{2})$", "", s) if re.search(r"T\d{2}:\d{2}", s) else s
    slash = ("%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y") if LOCALE["dayfirst"] else ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d") + slash + ("%Y%m%d",):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d")
    return None


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect(headers: set[str]) -> str:
    if {"line_item_product_code"} & headers:
        return "aws"
    if {"billed_cost", "charge_category"} <= headers or {"effective_cost", "service_name", "charge_category"} <= headers:
        return "focus"
    if {"product_service", "cost_my_cost"} <= headers:
        return "oci"
    if "service_name" in headers and "plan_name" in headers and ("usage_metric" in headers or "metric" in headers):
        return "ibm"
    if "meter_category" in headers:
        return "azure"
    if {"sku_description", "service_description"} <= headers:
        return "gcp"
    return "summary"

# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def to_hours(qty: float, unit: str) -> float:
    u = (unit or "").lower().strip()
    m = re.match(r"^(\d+)\s*(hour|hours|hrs?)$", u)
    if m:
        return qty * int(m.group(1))
    if u in ("hrs", "hr", "hour", "hours", "h", "1 hour"):
        return qty
    if u in ("seconds", "s", "second"):
        return qty / 3600
    if u in ("minutes", "min"):
        return qty / 60
    return qty


def to_gb_month(qty: float, unit: str, period_hours: float) -> float:
    u = (unit or "").lower().replace(" ", "")
    m = re.match(r"^(\d+)(gb|gib)/month$", u)
    if m:
        return qty * int(m.group(1))
    if u in ("gb-mo", "gb-month", "gibibytemonth", "gibibyte month", "gb/month", "1gb/month", "gib-month",
             "gibibyte-month", "gigabytemonth"):
        return qty
    if u in ("byte-seconds", "bytesecond", "byte-second"):
        return qty / 2**30 / (period_hours * 3600)
    if u in ("gibibytehour", "gibibyte-hour", "gib-hour", "gb-hours", "gb-hour", "gibibyte hour", "gibhour"):
        return qty / period_hours
    if u in ("tb/month", "1tb/month", "tb-mo", "tebibytemonth"):
        return qty * 1024
    if u.endswith("/month") and u.startswith("1"):
        return qty
    return qty


def to_gb(qty: float, unit: str) -> float:
    u = (unit or "").lower().replace(" ", "")
    if u in ("gb", "gib", "gibibyte", "1gb", "gigabyte", "gigabytes"):
        return qty
    m = re.match(r"^(\d+)(gb|gib)$", u)
    if m:
        return qty * int(m.group(1))
    if u in ("bytes", "byte"):
        return qty / 2**30
    if u in ("tb", "tib", "tebibyte", "1tb"):
        return qty * 1024
    return qty


AZURE_DISK_GB = {"1": 4, "2": 8, "3": 16, "4": 32, "6": 64, "10": 128, "15": 256, "20": 512, "30": 1024,
                 "40": 2048, "50": 4096, "60": 8192, "70": 16384, "80": 32767}

AZURE_APPSVC = {  # plan meter -> vcpu, ram
    "b1": (1, 1.75), "b2": (2, 3.5), "b3": (4, 7), "s1": (1, 1.75), "s2": (2, 3.5), "s3": (4, 7),
    "p1 v2": (1, 3.5), "p2 v2": (2, 7), "p3 v2": (4, 14), "p0 v3": (1, 4), "p1 v3": (2, 8),
    "p2 v3": (4, 16), "p3 v3": (8, 32), "p1mv3": (2, 16), "p2mv3": (4, 32), "p3mv3": (8, 64),
    "p4mv3": (16, 128), "p5mv3": (32, 256), "i1 v2": (2, 8), "i2 v2": (4, 16), "i3 v2": (8, 32),
    "p0v4": (1, 4), "p1v4": (2, 8), "p2v4": (4, 16), "p3v4": (8, 32),
}
AZURE_REDIS = {"c0": 0.25, "c1": 1, "c2": 2.5, "c3": 6, "c4": 13, "c5": 26, "c6": 53, "p1": 6, "p2": 13,
               "p3": 26, "p4": 53, "p5": 120}

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# (regex over a combined lower-case text, category). First match wins; provider-specific
# rules come first. The text is "service :: usage_type :: description :: extra".
RULES = [
    # ---- credits, tax, fees, discounts
    (r"\btax\b|\bvat\b", "tax"),
    (r"savingsplannegation|savings plan negation", "commitment"),
    (r"rifee|savingsplanrecurringfee|savingsplanupfrontfee|reservation fee|\bpurchase\b.*reserv|commitment v1|committed use", "commitment"),
    (r"edpdiscount|privateratediscount|bundleddiscount|enterprise discount|sppdiscount|distributor discount", "discount"),
    (r"\bcredits?\b|promotional|free trial|\brefund\b", "credit"),
    (r"licen[cs]es?\b|\bspla\b|\bbyol\b", "licence"),
    (r"unusedbox|unused reservation|unusedreservation", "commitment"),
    # ---- support
    (r"awssupport|support \(?(business|enterprise|developer)|^support\b|premier support|unified support|enhanced support", "support"),
    # ---- kubernetes control plane
    (r"amazoneks.*(perCluster|cluster)|eks-hours|kubernetes engine.*(cluster management|kubernetes clusters)|container service for kubernetes|"
     r"(zonal|regional|autopilot) kubernetes clusters|azure kubernetes service.*(uptime sla|standard|premium)", "kubernetes_control"),
    # ---- containers
    (r"fargate|autopilot pod|container instances|container apps|cloud run|app runner|app service|azure spring|code engine|"
     r"elastic container instance|serverless app engine", "container"),
    # ---- serverless
    (r"lambda|cloud functions|azure functions|functions.*execution|step functions|logic apps|function compute", "serverless"),
    # ---- data services
    (r"elasticache|memorystore|redis cache|cache for redis|managed redis|valkey|apsaradb for redis|\btair\b|databases for redis", "cache"),
    (r"opensearch|elasticsearch|amazones\b|esinstance|cognitive search|ai search", "search"),
    (r"amazonmsk|managed streaming for kafka|kafka\.|amazonmq|\bmq\.|event hubs|service bus|pub/?sub|sqs|sns\b|kinesis|eventbridge|awsevents|confluent|"
     r"event streams|message queue|rocketmq", "messaging"),
    (r"dynamodb|cosmos ?db|bigtable|spanner|firestore|datastore|documentdb|amazondocdb|keyspaces|neptune|\bdax\b|timestream|memorydb|"
     r"apsaradb for mongodb|tablestore|lindorm|cloudant|databases for mongodb", "nosql"),
    (r"redshift|bigquery|synapse|athena|\bemr\b|\bdataproc\b|\bdataflow\b|\bglue\b|databricks|fabric capacity|snowflake|looker|quicksight|"
     r"maxcompute|analyticdb|hologres|e-mapreduce|dataworks|watsonx\.data", "analytics"),
    (r"relational database service|amazonrds|aurora|cloud sql|azure database for|sql database|sql managed instance|\bdb\.|alloydb|azure sql|"
     r"apsaradb|polardb|databases for (postgresql|mysql|enterprisedb)|\bdb2\b", "database"),
    # ---- gpu / ml
    (r"sagemaker|vertex ai|machine learning|bedrock|azure openai|cognitive services|ai services|gemini|textract|rekognition|comprehend|transcribe|translate|"
     r"model studio|bailian|platform for ai|watsonx\.ai|watson", "ml_managed"),
    (r"gpu|nvidia|tesla|\bh100\b|\ba100\b|\bl4\b|\bt4\b", "gpu"),
    # ---- network
    (r"natgateway|nat gateway|cloud nat", "nat"),
    (r"cloudfront|front door|cloud cdn|azure cdn|\bcdn\b|content delivery network|dynamic route for cdn|internet services", "cdn"),
    (r"route ?53|cloud dns|azure dns|\bdns\b", "dns"),
    (r"loadbalanc|load balanc|lcuusage|application gateway|forwarding rule|awselb|elasticloadbalancing", "load_balancer"),
    (r"idleaddress|publicipv4|ip address|external ip|static ip|public ip", "ip"),
    (r"inter[ -]?zone|regional-bytes|datatransfer-regional|intra[- ]?region|inter availability zone|inter-az", "inter_az"),
    (r"inter[ -]?region|aws-out-bytes|aws-in-bytes|intercontinental|intra continent|inter continent|vnet peering|global peering|transit gateway|transitgateway|vpc peering|private ?link|vpcendpoint|vpc endpoint|direct ?connect|expressroute|interconnect|vpn", "network_other"),
    (r"datatransfer-out|data transfer out|internet egress|egress|download (worldwide|china|australia|apac|americas|emea)|bandwidth|\bout-bytes\b|standard data transfer|"
     r"internet shared bandwidth|elastic ip address", "egress"),
    (r"datatransfer-in|data transfer in", "free"),
    # ---- storage
    (r"key vault|\bkms\b|secrets? ?manager", "security"),
    (r"snapshot|backup|aws backup|recovery services|site recovery|backup vault", "backup"),
    (r"glacier|deep ?archive|gda-|archive storage|cold ?line|archive\b", "archive_storage"),
    (r"simple storage service|amazons3|cloud storage|blob|object storage|timedstorage|data lake storage|\bs3\b|requests-tier|\bgcs\b", "object_storage"),
    (r"amazonefs|\befs\b|fsx|filestore|azure files|\bfiles\b|netapp|file storage|apsara file storage|\bnas\b", "file_storage"),
    (r"elastic block store|ebs\b|ebs:|volumeusage|managed disks|persistent disk|\bpd capacity|hyperdisk|ssd backed pd|balanced pd|storage pd|\bdisk\b|volumep-iops|volumep-throughput|"
     r"elastic block storage|block storage for vpc|block volume", "block_storage"),
    # ---- monitoring & security
    (r"cloudwatch|cloud logging|cloud monitoring|log analytics|azure monitor|application insights|x-ray|cloudtrail|operations suite|stackdriver|managed prometheus|managed grafana|"
     r"log service|cloudmonitor|application real-time monitoring|cloud logs|activity tracker", "monitoring"),
    (r"guardduty|security ?hub|inspector|macie|defender|sentinel|security command center|waf|shield|kms|key vault|secrets ?manager|certificate|cloud armor|firewall|config\b|awsconfig|cloud key|"
     r"anti-ddos|security center|key protect|hyper protect", "security"),
    (r"cognito|entra|active directory|identity|iam identity", "identity"),
    (r"\bses\b|amazonses|simple email|communication services|sendgrid|pinpoint|amazon connect", "email"),
    (r"marketplace|awsmarketplace|3rd party|third party", "marketplace"),
    # ---- compute (last so specific services win)
    (r"elastic compute cloud|amazonec2|boxusage|spotusage|dedicatedusage|hostboxusage|virtual machines|compute engine|instance core|instance ram|\bvm\b|cpucredits|lightsail|elastic beanstalk|batch|"
     r"elastic compute service|virtual servers? for vpc|bare metal servers? for vpc|(?<!azure )kubernetes service|openshift on ibm", "compute"),
]
_RULES = [(re.compile(p, re.I), c) for p, c in RULES]


def classify(text: str) -> str:
    for rx, cat in _RULES:
        if rx.search(text):
            return cat
    return "other"


ENGINES = [("aurora postgres", "postgres"), ("aurora-postgresql", "postgres"), ("aurora mysql", "mysql"),
           ("aurora", "mysql"), ("postgres", "postgres"), ("mysql", "mysql"), ("mariadb", "mysql"),
           ("sql server", "sqlserver"), ("sqlserver", "sqlserver"), ("azure sql", "sqlserver"),
           ("sql database", "sqlserver"), ("sql managed instance", "sqlserver"), ("oracle", "oracle"),
           ("alloydb", "postgres"), ("db2", "db2"), ("redis", "redis"), ("valkey", "redis"),
           ("memcached", "memcached"), ("opensearch", "opensearch"), ("elasticsearch", "opensearch"),
           ("kafka", "kafka"), ("rabbitmq", "rabbitmq"), ("activemq", "activemq")]


def engine_of(text: str) -> str | None:
    t = text.lower()
    for k, v in ENGINES:
        if k in t:
            return v
    return None


def os_of(text: str) -> str:
    t = text.lower()
    if "windows" in t or ":0002" in t or ":0102" in t or ":0006" in t:
        return "windows"
    if "rhel" in t or "red hat" in t or ":0010" in t:
        return "rhel"
    if "suse" in t or "sles" in t:
        return "suse"
    return "linux"

# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


class Estate:
    NUMS = ("cost", "usage_hours", "vcpu_hours", "ram_gb_hours", "gpu_hours", "storage_gb_month",
            "transfer_gb", "requests", "rows")

    def __init__(self):
        self.items = {}
        self.resources = defaultdict(set)
        self.min_date = None
        self.max_date = None
        self.currencies = defaultdict(float)
        self.totals = defaultdict(float)  # by category, all costs incl. credits/tax
        self.unclassified = defaultdict(float)
        self.sources = []
        self.kinds = set()
        self.cost_basis = set()

    def add(self, *, category, service, cost, cloud, instance_type=None, engine=None, os_=None,
            pricing=None, usage_hours=0.0, vcpu_hours=0.0, ram_gb_hours=0.0, gpu_hours=0.0, gpu_model=None,
            storage_gb_month=0.0, storage_class=None, transfer_gb=0.0, requests=0.0, multi_az=False,
            replicas_included=False, standby=False, resource_id=None, description="", start=None, end=None, currency=None,
            series=None, cost_implied_ok=False):
        if currency:
            self.currencies[currency] += abs(cost)
        self.totals[category] += cost
        for d in (start, end):
            if d:
                self.min_date = d if self.min_date is None or d < self.min_date else self.min_date
                self.max_date = d if self.max_date is None or d > self.max_date else self.max_date
        if category == "other":
            self.unclassified[(service or "?")[:80] + " :: " + (description or "")[:80]] += cost
        key = (category, service, instance_type, engine, os_, pricing, gpu_model, storage_class, multi_az,
               replicas_included, standby, series)
        it = self.items.get(key)
        if it is None:
            it = {"category": category, "service": service, "cloud": cloud}
            for k, v in (("instance_type", instance_type), ("engine", engine), ("os", os_),
                         ("pricing_model", pricing), ("gpu_model", gpu_model), ("storage_class", storage_class),
                         ("series", series)):
                if v:
                    it[k] = v
            if multi_az:
                it["multi_az"] = True
            if replicas_included:
                it["replicas_included"] = True
            if standby:
                it["standby"] = True
            if cost_implied_ok:
                it["cost_implied_ok"] = True
            for n in self.NUMS:
                it[n] = 0.0
            it["description"] = (description or "")[:160]
            self.items[key] = it
        it["cost"] += cost
        it["usage_hours"] += usage_hours
        it["vcpu_hours"] += vcpu_hours
        it["ram_gb_hours"] += ram_gb_hours
        it["gpu_hours"] += gpu_hours
        it["storage_gb_month"] += storage_gb_month
        it["transfer_gb"] += transfer_gb
        it["requests"] += requests
        it["rows"] += 1
        if resource_id:
            self.resources[key].add(resource_id)

    def finish(self, period_hours_override=None, currency_override=None):
        if period_hours_override:
            period_hours = period_hours_override
        elif self.min_date and self.max_date and self.max_date > self.min_date:
            period_hours = max(24.0, (self.max_date - self.min_date).total_seconds() / 3600)
            # snap to whole days; billing end dates are usually exclusive midnights
            period_hours = round(period_hours / 24) * 24
        else:
            period_hours = 730.0
        months = round(period_hours / 730.0, 5)
        items = []
        for key, it in self.items.items():
            it["resources"] = len(self.resources.get(key, ()))
            # storage_gb_month is a sum over months; express as an average GB stock
            if it["storage_gb_month"]:
                # providers express GB-months against the actual month length, so divide by whole months
                it["storage_gb_avg"] = round(it["storage_gb_month"] / max(1, round(months)), 3)
            for n in self.NUMS:
                it[n] = round(it[n], 4)
            items.append({k: v for k, v in it.items() if v not in (0, 0.0, "", None) or k in ("cost",)})
        items.sort(key=lambda x: -abs(x["cost"]))
        currency = currency_override or (max(self.currencies, key=self.currencies.get) if self.currencies else "USD")
        excluded = {"tax", "credit"}
        total = sum(v for k, v in self.totals.items() if k not in excluded)
        return {
            "schema": SCHEMA,
            "meta": {
                "generated": dt.datetime.now().isoformat(timespec="seconds"),
                "input_files": self.sources,
                "input_kind": sorted(self.kinds),
                "cost_basis": sorted(self.cost_basis),
                "period_start": self.min_date.date().isoformat() if self.min_date else None,
                "period_end": self.max_date.date().isoformat() if self.max_date else None,
                "period_hours": period_hours,
                "months_covered": months,
                "currency": currency,
                "currencies_seen": {k: round(v, 2) for k, v in self.currencies.items()},
                "total_cost_ex_tax_ex_credits": round(total, 2),
                "credits": round(self.totals.get("credit", 0.0), 2),
                "tax": round(self.totals.get("tax", 0.0), 2),
                "cost_by_category": {k: round(v, 2) for k, v in sorted(self.totals.items(), key=lambda kv: -abs(kv[1]))},
                "unclassified_top": [{"what": k, "cost": round(v, 2)} for k, v in
                                     sorted(self.unclassified.items(), key=lambda kv: -abs(kv[1]))[:25]],
                "detail_level": "usage" if any(i.get("vcpu_hours") or i.get("usage_hours") for i in items) else "service",
            },
            "items": items,
        }

# ---------------------------------------------------------------------------
# AWS CUR (legacy and 2.0)
# ---------------------------------------------------------------------------


AWS_PRODUCT_CATEGORY = {
    "amazonefs": "file_storage", "amazonfsx": "file_storage", "amazons3": "object_storage",
    "amazons3glacierdeeparchive": "archive_storage", "amazonglacier": "archive_storage",
    "amazonrds": "database", "amazondocdb": "nosql", "amazonneptune": "nosql", "amazondynamodb": "nosql",
    "amazonkeyspaces": "nosql", "amazonmemorydb": "nosql", "amazontimestream": "nosql",
    "amazonelasticache": "cache", "amazones": "search", "amazonopensearchservice": "search",
    "amazonmsk": "messaging", "amazonmq": "messaging", "amazonsqs": "messaging", "amazonsns": "messaging",
    "amazonkinesis": "messaging", "amazonkinesisfirehose": "messaging", "awsevents": "messaging",
    "elasticmapreduce": "analytics", "amazonredshift": "analytics", "amazonathena": "analytics", "awsglue": "analytics",
    "amazonquicksight": "analytics", "amazoneks": "kubernetes_control", "amazonecs": "container",
    "awslambda": "serverless", "awsstepfunctions": "serverless", "amazoncloudfront": "cdn", "amazonroute53": "dns",
    "amazoncloudwatch": "monitoring", "awscloudtrail": "monitoring", "awsxray": "monitoring",
    "amazonsagemaker": "ml_managed", "amazonbedrock": "ml_managed", "awselb": "load_balancer",
    "amazonses": "email", "amazonguardduty": "security", "awskms": "security", "awssecretsmanager": "security",
    "awssecurityhub": "security", "awswaf": "security", "awsshield": "security", "awsconfig": "security",
    "amazoninspectorv2": "security", "amazonmacie": "security", "amazoncognito": "identity",
    "awsbackup": "backup", "awsdirectconnect": "network_other", "awstransfer": "network_other",
    "amazonvpc": "network_other", "awsglobalaccelerator": "network_other", "amazonlightsail": "compute",
    "amazonworkspaces": "other", "awsmarketplace": "marketplace",
}
# usage-type patterns that cut across products and win over the product code
AWS_USAGE_CATEGORY = [
    (r"datatransfer-out-bytes|-out-bytes$|cloudfront-out|datatransfer-out", "egress"),
    (r"datatransfer-regional-bytes", "inter_az"),
    (r"-aws-out-bytes|-aws-in-bytes|datatransfer-xaz|interregion", "network_other"),
    (r"datatransfer-in-bytes|-in-bytes$", "free"),
    (r"natgateway", "nat"),
    (r"publicipv4|elasticip|idleaddress", "ip"),
    (r"ebs:snapshot|snapshotusage|snapshotarchive", "backup"),
    (r"ebs:volume|ebs:directapi", "block_storage"),
    (r"vpcendpoint|transitgateway|vpn-usage", "network_other"),
]


def aws_category(code: str, usage_type: str, desc: str) -> str:
    ut = usage_type.lower()
    c = code.lower().replace(" ", "")
    if c not in ("amazoncloudfront",):
        for rx, cat in AWS_USAGE_CATEGORY:
            if re.search(rx, ut):
                return cat
    if c.startswith("awssupport") or c.startswith("awsdeveloper"):
        return "support"
    if c in AWS_PRODUCT_CATEGORY:
        cat = AWS_PRODUCT_CATEGORY[c]
        if cat == "object_storage" and re.search(r"glacier|gda-|deeparchive", ut):
            return "archive_storage"
        if cat == "monitoring" and "dataprocessing" in ut:
            return "monitoring"
        return cat
    if c == "amazonec2":
        if re.search(r"boxusage|spotusage|dedicatedusage|hostboxusage|hostusage|cpucredits|unusedbox|reservedhost", ut):
            return "compute"
    cat = classify(f"{code} :: {usage_type} :: {desc}")
    if cat in ("credit", "tax", "discount"):  # only line_item_type decides these for AWS
        cat = classify(f"{code} :: {usage_type}")
        if cat in ("credit", "tax", "discount"):
            cat = "other"
    return cat


def _product_attr(row, name):
    v = first(row, f"product_{name}")
    if v:
        return v
    prod = row.get("product")
    if prod and prod.startswith("{"):
        try:
            return json.loads(prod).get(name, "")
        except json.JSONDecodeError:
            return ""
    return ""


def ingest_aws(rows, est: Estate, headers):
    amortised = "reservation_effective_cost" in headers or "savings_plan_savings_plan_effective_cost" in headers
    net = "line_item_net_unblended_cost" in headers
    est.cost_basis.add("aws:amortised" if amortised else ("aws:net_unblended" if net else "aws:unblended"))
    for r in rows:
        lit = first(r, "line_item_line_item_type")
        code = first(r, "line_item_product_code", "product_servicecode")
        usage_type = first(r, "line_item_usage_type")
        desc = first(r, "line_item_line_item_description")
        qty = fnum(first(r, "line_item_usage_amount"))
        unit = first(r, "pricing_unit")
        base = fnum(first(r, "line_item_net_unblended_cost") if net else first(r, "line_item_unblended_cost"))
        cost = base
        if amortised:
            if lit == "DiscountedUsage":
                cost = fnum(first(r, "reservation_net_effective_cost", "reservation_effective_cost"))
            elif lit == "SavingsPlanCoveredUsage":
                cost = fnum(first(r, "savings_plan_net_savings_plan_effective_cost", "savings_plan_savings_plan_effective_cost"))
            elif lit in ("RIFee", "SavingsPlanRecurringFee", "SavingsPlanNegation", "SavingsPlanUpfrontFee"):
                # already spread into the usage rows above; keep only unused commitment
                if lit == "RIFee":
                    cost = fnum(first(r, "reservation_unused_amortized_upfront_fee_for_billing_period")) + \
                           fnum(first(r, "reservation_unused_recurring_fee"))
                else:
                    cost = 0.0
            elif lit == "Fee" and "upfront" in desc.lower():
                cost = 0.0
        start = parse_date(first(r, "line_item_usage_start_date", "identity_time_interval").split("/")[0])
        end = parse_date(first(r, "line_item_usage_end_date"))
        currency = first(r, "line_item_currency_code", "pricing_currency")
        rid = first(r, "line_item_resource_id")
        text = f"{code} :: {usage_type} :: {desc} :: {lit}"
        if lit in ("Tax",):
            cat = "tax"
        elif lit in ("Credit", "Refund"):
            cat = "credit"
        elif lit in ("EdpDiscount", "PrivateRateDiscount", "BundledDiscount", "Discount", "SppDiscount", "DistributorDiscount"):
            cat = "discount"
        elif lit in ("RIFee", "SavingsPlanRecurringFee", "SavingsPlanUpfrontFee", "SavingsPlanNegation") or \
                (lit == "Fee" and ("reserv" in desc.lower() or "savings plan" in desc.lower())):
            cat = "commitment"
        else:
            cat = aws_category(code, usage_type, desc)
        ut = usage_type.lower()
        kw = dict(category=cat, service=code, cost=cost, cloud="aws", resource_id=rid, description=desc,
                  start=start, end=end, currency=currency)
        itype = _product_attr(r, "instance_type") or find_instance_type(usage_type) or find_instance_type(desc)
        if "unusedbox" in ut or "unusedreservation" in ut:
            kw["category"] = cat = "commitment"
        pricing = "spot" if "spot" in ut else ("reserved" if lit == "DiscountedUsage" else
                                               "savings_plan" if lit == "SavingsPlanCoveredUsage" else "on_demand")
        if cat in ("compute", "database", "cache", "search", "messaging", "gpu") and \
                itype and re.search(r"boxusage|spotusage|dedicatedusage|hostboxusage|instanceusage|multi-azusage|"
                                    r"nodeusage|esinstance|usage:|instance|node", ut + " " + desc.lower()):
            spec = decode(itype, "aws")
            hours = qty if (unit or "hrs").lower() in ("hrs", "hours", "hour", "") else to_hours(qty, unit)
            if spec and spec["gpu_count"] and cat == "compute":
                cat = kw["category"] = "gpu"
            kw.update(instance_type=itype, pricing=pricing, usage_hours=hours,
                      engine=engine_of(f"{_product_attr(r, 'database_engine')} {desc} {code} {_product_attr(r, 'cache_engine')}") if cat != "compute" and cat != "gpu" else None,
                      os_=os_of(f"{_product_attr(r, 'operating_system')} {desc} {first(r, 'line_item_operation')}") if cat in ("compute", "gpu") else None,
                      multi_az="multi-az" in ut or "multi-az" in desc.lower(),
                      replicas_included="aurora" in desc.lower() or "aurora" in _product_attr(r, "database_engine").lower())
            if spec:
                kw.update(vcpu_hours=hours * spec["vcpu"], ram_gb_hours=hours * spec["ram_gb"],
                          gpu_hours=hours * spec["gpu_count"], gpu_model=spec["gpu_model"])
        elif cat == "container" and "fargate" in ut:
            if "vcpu" in ut:
                kw.update(vcpu_hours=qty)
            elif "gb-hours" in ut or "gb" in ut:
                kw.update(ram_gb_hours=qty)
            kw.update(service="Fargate")
        elif cat == "serverless" and "lambda" in code.lower():
            if "gb-second" in ut:
                kw.update(ram_gb_hours=qty / 3600, vcpu_hours=qty / 3600 / 1.769)
            elif "request" in ut:
                kw.update(requests=qty)
        elif cat == "database" and "serverlessv2" in ut.replace(" ", ""):
            kw.update(ram_gb_hours=qty * 2, vcpu_hours=qty * 0.25, engine=engine_of(desc + " aurora"), replicas_included=True,
                      instance_type="aurora-serverless-v2")
        elif cat == "kubernetes_control":
            kw.update(usage_hours=qty)
        elif cat in ("database", "search", "cache") and "backup" in ut:
            kw.update(category="backup", storage_gb_month=qty, storage_class="snapshot")
        elif cat in ("block_storage", "object_storage", "file_storage", "backup", "archive_storage") or \
                (cat in ("database", "search", "cache") and re.search(r"storage|gp2|gp3|piops|io1|io2|volume", ut)):
            if re.search(r"timedstorage|storageusage|volumeusage|byte-?hrs|gb-mo|storage|snapshotusage|backupusage", ut) and \
                    not re.search(r"iops|throughput|requests|tier\d", ut):
                kw.update(storage_gb_month=qty, storage_class=_aws_storage_class(ut))
                if cat in ("database", "search", "cache"):
                    kw.update(category=f"{cat}_storage", engine=engine_of(desc + " " + code))
            elif "requests" in ut:
                kw.update(requests=qty)
        elif cat in ("egress", "inter_az", "network_other", "cdn", "nat", "free"):
            if "bytes" in ut or unit.lower() == "gb":
                kw.update(transfer_gb=qty)
        if cat == "free":
            continue
        est.add(**kw)


def _aws_storage_class(ut: str) -> str:
    for k, v in (("gp3", "ssd"), ("gp2", "ssd"), ("piops", "ssd_iops"), ("io1", "ssd_iops"), ("io2", "ssd_iops"),
                 ("st1", "hdd"), ("sc1", "hdd"), ("snapshot", "snapshot"), ("glacier", "archive"),
                 ("gda", "deep_archive"), ("sia", "infrequent"), ("zia", "infrequent"), ("int-", "intelligent"),
                 ("timedstorage", "standard")):
        if k in ut:
            return v
    return "standard"

# ---------------------------------------------------------------------------
# Azure Cost Management export
# ---------------------------------------------------------------------------


def ingest_azure(rows, est: Estate, headers):
    est.cost_basis.add("azure:" + ("amortised_or_actual"))
    for r in rows:
        mc = first(r, "meter_category", "service_family")
        msc = first(r, "meter_sub_category")
        mn = first(r, "meter_name")
        product = first(r, "product_name", "product")
        unit = first(r, "unit_of_measure")
        qty = fnum(first(r, "quantity", "consumed_quantity"))
        cost = fnum(first(r, "cost_in_billing_currency", "cost", "pre_tax_cost", "cost_in_usd", "extended_cost"))
        currency = first(r, "billing_currency_code", "billing_currency", "currency")
        charge = first(r, "charge_type").lower()
        rid = first(r, "resource_id", "instance_id", "instance_name")
        d = parse_date(first(r, "date", "usage_date", "usage_date_time"))
        info_raw = first(r, "additional_info")
        info = {}
        if info_raw.startswith("{"):
            try:
                info = json.loads(info_raw)
            except json.JSONDecodeError:
                info = {}
        text = f"{mc} :: {msc} :: {mn} :: {product} :: {charge}"
        if charge in ("purchase",) and "reserv" in (first(r, "pricing_model") + text).lower():
            cat = "commitment"
        elif charge == "unusedreservation" or charge == "unusedsavingsplan":
            cat = "commitment"
        elif charge in ("refund",):
            cat = "credit"
        elif charge in ("tax",):
            cat = "tax"
        else:
            cat = classify(text)
        kw = dict(category=cat, service=mc, cost=cost, cloud="azure", resource_id=rid, description=f"{msc} {mn}".strip(),
                  start=d, end=(d + dt.timedelta(days=1)) if d else None, currency=currency)
        lowtext = text.lower()
        pricing = "spot" if ("spot" in lowtext or "low priority" in lowtext) else (
            "reserved" if "reservation" in (first(r, "pricing_model") or "").lower() else "on_demand")
        if cat in ("compute", "gpu") and mc.lower().startswith("virtual machines"):
            size = info.get("ServiceType") or find_instance_type(mn, "azure") or find_instance_type(product, "azure")
            hours = to_hours(qty, unit)
            spec = decode(size, "azure") if size else None
            if spec and spec["gpu_count"]:
                kw["category"] = "gpu"
            kw.update(instance_type=size, usage_hours=hours, pricing=pricing,
                      os_=os_of(f"{msc} {product} {info.get('ImageType', '')}"))
            if spec:
                kw.update(vcpu_hours=hours * spec["vcpu"], ram_gb_hours=hours * spec["ram_gb"],
                          gpu_hours=hours * spec["gpu_count"], gpu_model=spec["gpu_model"])
        elif cat == "database" and "vcore" in mn.lower():
            hours = to_hours(qty, unit)
            ratio = 8 if "memory optimized" in lowtext or "business critical" in lowtext and "m-series" in lowtext else (
                5.1 if "sql" in mc.lower() else 4)
            if "burstable" in lowtext:
                ratio = 2
            kw.update(vcpu_hours=hours, ram_gb_hours=hours * ratio, usage_hours=hours,
                      engine=engine_of(text), multi_az="ha" in lowtext.split() or "zone redundan" in lowtext,
                      standby="standby" in lowtext or "secondary" in lowtext)
        elif cat == "database" and re.search(r"storage|backup", mn.lower()):
            kw.update(category="database_storage" if "backup" not in mn.lower() else "backup",
                      storage_gb_month=to_gb_month(qty, unit, 730), engine=engine_of(text))
        elif cat == "container" and "app service" in mc.lower():
            plan = re.sub(r"\s*app$", "", mn.lower()).strip()
            plan = plan.replace("premium ", "p").strip()
            spec = AZURE_APPSVC.get(plan) or AZURE_APPSVC.get(plan.replace(" ", ""))
            hours = to_hours(qty, unit)
            kw.update(usage_hours=hours, instance_type=f"AppService {mn}")
            if spec:
                kw.update(vcpu_hours=hours * spec[0], ram_gb_hours=hours * spec[1])
        elif cat == "container" and re.search(r"vcpu|memory|gib", mn.lower()):
            per = 1 / 3600 if "second" in unit.lower() or unit.lower() in ("s", "1 s", "seconds") else 1
            if "vcpu" in mn.lower():
                kw.update(vcpu_hours=to_hours(qty, unit) if per == 1 else qty * per)
            else:
                kw.update(ram_gb_hours=to_hours(qty, unit) if per == 1 else qty * per)
        elif cat == "cache":
            tier = re.match(r"^([cp]\d)", mn.lower())
            hours = to_hours(qty, unit)
            if tier and tier.group(1) in AZURE_REDIS:
                gb = AZURE_REDIS[tier.group(1)]
                kw.update(ram_gb_hours=hours * gb * (2 if mn.lower().startswith("p") or "standard" in lowtext else 1),
                          vcpu_hours=hours * max(1, gb / 6), usage_hours=hours, engine="redis", instance_type=f"Redis {mn}",
                          replicas_included=True)
        elif cat == "serverless" and "execution time" in mn.lower():
            kw.update(ram_gb_hours=qty / 3600, vcpu_hours=qty / 3600 / 1.5)
        elif cat == "block_storage":
            m = re.match(r"^([pes])(\d+)\b", mn.lower())
            if m and m.group(2) in AZURE_DISK_GB:
                # qty is in disk-months ("1/Month"); some exports bill per day or hour
                ul = unit.lower()
                disk_months = qty / 30.4167 if "day" in ul else (qty / 730 if "hour" in ul else qty)
                kw.update(storage_gb_month=disk_months * AZURE_DISK_GB[m.group(2)],
                          storage_class="ssd" if m.group(1) in "pe" else "hdd")
            elif re.search(r"gib|gb", unit.lower()) or "provisioned capacity" in mn.lower():
                kw.update(storage_gb_month=to_gb_month(qty, unit, 730) if "hour" not in unit.lower() else qty / 730,
                          storage_class="ssd")
        elif cat in ("object_storage", "file_storage", "backup", "archive_storage"):
            if "data stored" in mn.lower() or "capacity" in mn.lower() or re.search(r"gb/month|gib/month", unit.lower()):
                tier = "archive" if "archive" in lowtext else ("cool" if "cool" in lowtext or "cold" in lowtext else "standard")
                if tier == "archive":
                    kw["category"] = "archive_storage"
                kw.update(storage_gb_month=to_gb_month(qty, unit, 730), storage_class=tier)
        elif cat in ("egress", "inter_az", "network_other", "cdn"):
            if re.search(r"gb|gib", unit.lower()):
                kw.update(transfer_gb=to_gb(qty, unit))
        elif cat == "kubernetes_control":
            kw.update(usage_hours=to_hours(qty, unit))
        est.add(**kw)

# ---------------------------------------------------------------------------
# Google Cloud billing export
# ---------------------------------------------------------------------------

GCP_GPU_SKU = [("h100", "H100"), ("h200", "H200"), ("a100 80", "A100-80"), ("a100", "A100-40"), ("l4", "L4"),
               ("t4", "T4"), ("v100", "V100"), ("p100", "P100"), ("p4", "P4"), ("b200", "B200")]


def ingest_gcp(rows, est: Estate, headers):
    est.cost_basis.add("gcp:cost_plus_credits_separate")
    for r in rows:
        svc = first(r, "service_description")
        sku = first(r, "sku_description")
        cost = fnum(first(r, "cost"))
        currency = first(r, "currency")
        unit = first(r, "usage_pricing_unit", "usage_unit")
        qty = fnum(first(r, "usage_amount_in_pricing_units")) if first(r, "usage_amount_in_pricing_units") else fnum(first(r, "usage_amount"))
        if not first(r, "usage_amount_in_pricing_units"):
            unit = first(r, "usage_unit")
        start = parse_date(first(r, "usage_start_time"))
        end = parse_date(first(r, "usage_end_time"))
        rid = first(r, "resource_global_name", "resource_name")
        text = f"{svc} :: {sku}"
        low = text.lower()
        cat = classify(text)
        if svc.lower() == "compute engine" and re.search(r"core|ram|gpu|instance", sku.lower()) and "commitment" not in sku.lower():
            cat = "gpu" if re.search(r"gpu|nvidia", sku.lower()) else "compute"
        if svc.lower().startswith("support"):
            cat = "support"
        kw = dict(category=cat, service=svc, cost=cost, cloud="gcp", resource_id=rid, description=sku,
                  start=start, end=end, currency=currency)
        series = None
        sm = re.match(r"^(?:spot preemptible |preemptible |commitment v1: )?([A-Za-z0-9]+)\b", sku)
        if sm:
            series = sm.group(1).lower()
        u = unit.lower()
        hours = qty / 3600 if u in ("seconds", "s") else qty
        if cat in ("compute", "database") and re.search(r"\b(core|vcpu|cpu)\b", sku.lower()) and "commit" not in sku.lower():
            kw.update(vcpu_hours=hours, series=series if cat == "compute" else None,
                      pricing="spot" if re.search(r"spot|preemptible", sku.lower()) else "on_demand")
            if cat == "database":
                kw.update(engine=engine_of(text), multi_az="regional" in low or "high availability" in low)
        elif cat in ("compute", "database") and re.search(r"\bram\b|memory", sku.lower()) and "commit" not in sku.lower():
            gbh = qty / 3600 / 2**30 if "byte-seconds" in u else (qty / 3600 if u in ("gibibyte second",) else hours)
            kw.update(ram_gb_hours=gbh, series=series if cat == "compute" else None,
                      pricing="spot" if re.search(r"spot|preemptible", sku.lower()) else "on_demand")
            if cat == "database":
                kw.update(engine=engine_of(text), multi_az="regional" in low or "high availability" in low)
        elif cat == "gpu":
            model = next((v for k, v in GCP_GPU_SKU if k in sku.lower()), "unknown")
            kw.update(gpu_hours=hours, gpu_model=model)
        elif cat == "container":
            if re.search(r"cpu|core", sku.lower()):
                factor = 1 / 1000 if "mcpu" in sku.lower() else 1
                kw.update(vcpu_hours=(qty / 3600 if u in ("seconds", "s", "vcpu-seconds") else qty) * factor)
            elif re.search(r"memory|ram", sku.lower()):
                kw.update(ram_gb_hours=qty / 3600 if "second" in u else qty)
        elif cat == "cache" and "capacity" in sku.lower():
            kw.update(ram_gb_hours=qty if "hour" in u else qty / 3600, vcpu_hours=(qty if "hour" in u else qty / 3600) / 6,
                      engine="redis", replicas_included="standard" in sku.lower())
        elif cat in ("block_storage", "object_storage", "file_storage", "backup", "archive_storage") or \
                (cat == "database" and "storage" in sku.lower()):
            if re.search(r"byte|gibibyte|month|gb", u) and not re.search(r"operation|request|retrieval|class [ab]", sku.lower()):
                gbm = to_gb_month(qty, unit, 730)
                if "byte-seconds" in u:
                    gbm = qty / 2**30 / (730 * 3600)
                if cat == "database":
                    kw.update(category="database_storage", engine=engine_of(text))
                cls = "archive" if re.search(r"archive|coldline", sku.lower()) else (
                    "nearline" if "nearline" in sku.lower() else ("ssd" if "ssd" in sku.lower() else "standard"))
                if cls == "archive" and cat == "object_storage":
                    kw["category"] = "archive_storage"
                kw.update(storage_gb_month=gbm, storage_class=cls)
        elif cat in ("egress", "inter_az", "network_other", "cdn", "nat"):
            if re.search(r"byte|gib|gb", u):
                kw.update(transfer_gb=qty / 2**30 if u in ("bytes", "byte") else to_gb(qty, unit))
        elif cat == "kubernetes_control":
            kw.update(usage_hours=hours)
        est.add(**kw)
        # credits: JSON list of {name, amount, type?}
        cr = first(r, "credits")
        if cr and cr.strip().startswith("["):
            try:
                for c in json.loads(cr):
                    amt = fnum(c.get("amount"))
                    if not amt:
                        continue
                    name = (c.get("name") or "") + " " + (c.get("type") or "")
                    if re.search(r"sustained|committed|commitment|discount|resell|subscription", name, re.I):
                        est.add(category="discount", service=svc, cost=amt, cloud="gcp", description=name.strip(),
                                currency=currency)
                    else:
                        est.add(category="credit", service=svc, cost=amt, cloud="gcp", description=name.strip(),
                                currency=currency)
            except (json.JSONDecodeError, AttributeError):
                pass

# ---------------------------------------------------------------------------
# Oracle Cloud Infrastructure: cost report (product/service, cost/myCost ...) and the
# OCI service codes, shared with OCI's FOCUS reports (ServiceName = product/service)
# ---------------------------------------------------------------------------

# product/service codes as they appear in OCI cost and FOCUS reports
OCI_SERVICE_CATEGORY = {
    "COMPUTE": "compute", "BLOCK_STORAGE": "block_storage", "OBJECTSTORE": "object_storage",
    "FILESTORAGESERVICE": "file_storage", "ORALB": "load_balancer", "OKE_CONTROL_PLANE": "kubernetes_control",
    "ODSP": "database", "OPENSEARCH": "search", "ORACLE_STREAMING_SERVICE": "messaging",
    "IDENTITY_AND_ACCESS_MANAGEMENT": "identity", "TELEMETRY": "monitoring",
}
# description patterns (product/description is the SKU name) that win over the service code
OCI_DESC_CATEGORY = [
    (r"windows os|sql server|nvidia ai enterprise|weblogic|\bbyol\b", "licence"),
    (r"outbound data transfer.*(inter|region)|inter.?region|fastconnect|site-to-site|vpn", "network_other"),
    (r"outbound data transfer", "egress"),
    (r"archive storage", "archive_storage"),
    (r"backup", "backup"),
    (r"gpu", "gpu"),
    (r"image storage", "object_storage"),
    (r"load balancer", "load_balancer"),
    (r"kubernetes engine.*virtual node|container instance", "container"),
    (r"kubernetes|container engine", "kubernetes_control"),
    (r"autonomous|exadata|base database|oracle database|mysql|heatwave|postgresql|timesten", "database"),
    (r"cache with redis|redis", "cache"),
    (r"opensearch", "search"),
    (r"streaming|kafka|queue|notifications", "messaging"),
    (r"functions", "serverless"),
    (r"logging|log analytics|monitoring|apm\b", "monitoring"),
    (r"\bdns\b", "dns"),
    (r"email delivery", "email"),
    (r"vault|key management|waf|web application firewall|network firewall|cloud guard|data safe|bastion|certificate", "security"),
    (r"identity|\biam\b", "identity"),
    (r"generative ai|language|vision|speech|document understanding|data science", "ml_managed"),
    (r"analytics cloud|data integration|data flow|big data|data catalog", "analytics"),
    (r"nosql", "nosql"),
]
_OCI_DESC = [(re.compile(p, re.I), c) for p, c in OCI_DESC_CATEGORY]
OCI_GPU = [("a100 - v2", "A100-80"), ("a100-v2", "A100-80"), ("a100 80", "A100-80"), ("a100 40", "A100-40"),
           ("h100", "H100"), ("h200", "H200"), ("b200", "B200"), ("l40s", "L40S"), ("a10", "A10"),
           ("gpu standard - v2", "V100"), ("gpu standard - x7", "P100")]


def oci_line(service: str, desc: str, unit: str, qty: float, period_hours: float = 730.0) -> dict:
    """Category and usage quantities for one OCI cost line, from its service code, SKU name and unit."""
    svc = (service or "").strip().upper()
    d = (desc or "").lower()
    u = (unit or "").lower()
    cat = None
    for rx, c in _OCI_DESC:
        if rx.search(d):
            cat = c
            break
    base = OCI_SERVICE_CATEGORY.get(svc)
    if cat is None:
        cat = base or classify(f"{service} :: {desc}")
    elif cat == "gpu" and base and base != "compute":
        cat = base  # GPU-backed shapes of a managed service stay with the service
    if base in ("database", "search", "cache") and cat in ("compute", "object_storage"):
        cat = base
    kw = {"category": cat}
    ocpu = "ocpu" in u and "fee" not in u
    gb_hours = re.search(r"\b(gigabytes?|gb)( per | )hours?\b|gigabyte per hour", u) and "redis" not in u
    series = None
    if cat in ("compute", "database", "search", "container") and (ocpu or gb_hours):
        series = oci_series(desc)
        per, smt, _gen = oci_traits(series)
        if ocpu:
            kw["vcpu_hours"] = qty * per
        else:
            kw["ram_gb_hours"] = qty
        kw["series"] = f"oci-{series}" if series and cat == "compute" else None
        if cat == "database":
            kw["engine"] = engine_of(desc) or ("postgres" if svc == "ODSP" else None)
    elif cat == "database" and re.search(r"ecpu", u):
        kw["cost_implied_ok"] = True  # ECPUs are an abstract unit; size from cost at list prices
        kw["engine"] = "oracle" if re.search(r"autonomous|exadata|base database|oracle database", d) else engine_of(desc)
    elif cat == "database":
        kw["engine"] = "oracle" if re.search(r"autonomous|exadata|base database|oracle database", d) else (
            engine_of(desc) or ("postgres" if svc == "ODSP" else None))
        if "storage" in d and "month" in u:
            kw.update(category="database_storage", storage_gb_month=qty)
        elif "heatwave" in d or "capacity per hour" in u:
            kw["cost_implied_ok"] = True
    elif cat == "search" and "storage" in d and "month" in u:
        kw.update(category="search_storage", storage_gb_month=qty, engine="opensearch")
    elif cat == "cache" and "redis" in u:
        kw.update(ram_gb_hours=qty, vcpu_hours=qty / 6, engine="redis")
    elif cat == "gpu":
        kw.update(gpu_hours=qty if "gpu" in u else 0.0,
                  gpu_model=next((v for k, v in OCI_GPU if k in d), "unknown"))
    elif cat in ("block_storage", "object_storage", "file_storage", "archive_storage", "backup"):
        if "month" in u and re.search(r"\bg(b|igabyte)", u) and "performance unit" not in d:
            kw.update(storage_gb_month=qty * (1024 if re.search(r"\bt(b|erabyte)", u) else 1),
                      storage_class="archive" if cat == "archive_storage" else (
                          "infrequent" if "infrequent" in d else "standard"))
        elif "request" in u:
            kw["requests"] = qty
    elif cat in ("egress", "network_other"):
        if re.search(r"\bg(b|igabyte)|\bt(b|erabyte)", u):
            kw["transfer_gb"] = qty * (1024 if re.search(r"\bt(b|erabyte)", u) else 1)
    elif cat == "kubernetes_control" and re.search(r"cluster", u):
        kw["usage_hours"] = qty
    return {k: v for k, v in kw.items() if v not in (None, 0.0) or k == "category"}


def ingest_oci(rows, est: Estate, headers):
    """OCI cost report CSV (Billing and Cost Management > Cost and Usage Reports)."""
    est.cost_basis.add("oci:my_cost")
    for r in rows:
        svc = first(r, "product_service")
        desc = first(r, "product_description")
        unit = first(r, "cost_sku_unit_description", "cost_billing_unit_readable")
        qty = fnum(first(r, "usage_billed_quantity"))
        cost = fnum(first(r, "cost_my_cost"))
        currency = first(r, "cost_currency_code")
        start = parse_date(first(r, "line_item_interval_usage_start"))
        end = parse_date(first(r, "line_item_interval_usage_end"))
        rid = first(r, "product_resource_id")
        kw = oci_line(svc, desc, unit, qty)
        est.add(service=svc or "OCI", cost=cost, cloud="oci", resource_id=rid, description=desc,
                start=start, end=end, currency=currency, **kw)

# ---------------------------------------------------------------------------
# IBM Cloud usage CSV (Billing and usage > Usage > Export CSV: "Instances", or the
# Account Resource Usage section of the "Summary" export)
# ---------------------------------------------------------------------------

IBM_SERVICE_CATEGORY = [
    (r"windows_vcpu|redhat_vcpu|rhel_vcpu|os_|_os_|licen[cs]e|sql_server", "licence"),
    (r"bandwidth", "egress"),
    (r"kubernetes service|openshift", "compute"),        # worker nodes are VMs; IKS has no control-plane fee
    (r"virtual server|bare metal server", "compute"),
    (r"file storage", "file_storage"),
    (r"block storage|storagelayer", "block_storage"),
    (r"cloud object storage", "object_storage"),
    (r"databases for (redis|valkey)", "cache"),
    (r"databases for (elasticsearch|opensearch)", "search"),
    (r"databases for (mongodb|etcd)|cloudant", "nosql"),
    (r"databases for|db2|mysql|postgres", "database"),
    (r"load balancer", "load_balancer"),
    (r"floating ip|public gateway", "ip"),
    (r"transit gateway|direct link|vpn", "network_other"),
    (r"event streams|mq\b|messaging", "messaging"),
    (r"code engine", "container"),
    (r"cloud functions", "serverless"),
    (r"log analysis|cloud logs|monitoring|activity tracker|metrics routing", "monitoring"),
    (r"key protect|hyper protect|secrets manager|security and compliance|certificate", "security"),
    (r"internet services", "cdn"),
    (r"dns services", "dns"),
    (r"app id", "identity"),
    (r"watson|watsonx", "ml_managed"),
    (r"support", "support"),
]
_IBM = [(re.compile(p, re.I), c) for p, c in IBM_SERVICE_CATEGORY]
IBM_SHAPE = re.compile(r"(?:^|[^\w.])(\d{1,3})x(\d{1,4})(?:\b|-)")


def ibm_line(service, plan, metric, unit, qty, profile, period_hours):
    """Category and quantities for one IBM Cloud usage line."""
    text = f"{service} :: {plan} :: {metric}"
    m_ = (metric or "").upper()
    u = (unit or "").upper()
    cat = next((c for rx, c in _IBM if rx.search(text)), None) or classify(text)
    if cat == "object_storage" and re.search(r"COLD_VAULT|ARCHIVE|GLACIER", m_):
        cat = "archive_storage" if "STORAGE" in m_ else cat
    if cat in ("compute", "block_storage") and re.search(r"(_|^)(storage|disk)", m_) and "GUEST_DISK" not in m_:
        cat = "block_storage"
    kw = {"category": cat}
    if u == "CHARGE":
        # classic infrastructure: the quantity is money; cores and RAM are in the metric name and billed monthly
        c = re.match(r"GUEST_CORES_(\d+)", m_)
        r = re.match(r"RAM_(\d+)_GB", m_)
        if cat == "compute" and c:
            kw["vcpu_hours"] = int(c.group(1)) * period_hours
        elif cat == "compute" and r:
            kw["ram_gb_hours"] = int(r.group(1)) * period_hours
        return kw
    hours = "HOUR" in m_ or "HOUR" in u
    if cat == "compute" and hours:
        spec = decode(profile, "ibm") if profile else None
        shape = None if spec else IBM_SHAPE.search(f" {plan} ")
        if spec:
            kw.update(instance_type=profile, usage_hours=qty, vcpu_hours=qty * spec["vcpu"], ram_gb_hours=qty * spec["ram_gb"])
        elif shape:
            kw.update(usage_hours=qty, vcpu_hours=qty * int(shape.group(1)), ram_gb_hours=qty * int(shape.group(2)))
        else:
            kw.update(usage_hours=qty, cost_implied_ok=True)
    elif cat in ("database", "cache", "search", "nosql"):
        kw["engine"] = engine_of(text)
        kw["replicas_included"] = True  # IBM Cloud Databases bill every member's cores, RAM and disk
        if re.search(r"VIRTUAL_PROCESSOR_CORE|VCPU", m_):
            kw["vcpu_hours"] = qty  # virtual processor core-hours
        elif "RAM" in m_ or "MEMORY" in m_:
            kw["ram_gb_hours"] = qty * period_hours if "MONTH" in m_ else qty
        elif "DISK" in m_ or "STORAGE" in m_:
            gbm = qty / period_hours if "HOUR" in m_ else qty
            kw.update(category=f"{cat}_storage" if cat in ("database", "search", "cache") else cat, storage_gb_month=gbm)
        else:
            kw["cost_implied_ok"] = True  # host flavours (HOST_FOUR_SIXTEEN_FLEX ...) carry no usable quantity
    elif cat in ("block_storage", "object_storage", "archive_storage", "file_storage", "backup"):
        if re.search(r"STORAGE|GIGABYTE_(HOURS|MONTHS)|CAPACITY", m_) and not re.search(r"CALLS|RETRIEVAL|IOPS", m_):
            kw["storage_gb_month"] = qty / period_hours if "HOUR" in m_ or "HOUR" in u else qty
    elif cat == "egress" and re.search(r"GIGABYTE|GB", u + " " + m_):
        kw["transfer_gb"] = qty
    return kw


def ingest_ibm(rows, est: Estate, headers):
    est.cost_basis.add("ibm:cost")
    start = end = None
    bm = FILE_META.get("billing_month", "")
    if re.fullmatch(r"\d{4}-\d{2}", bm):
        start = dt.datetime.strptime(bm + "-01", "%Y-%m-%d")
        end = (start + dt.timedelta(days=32)).replace(day=1)
    period_hours = (end - start).total_seconds() / 3600 if start else 730.0
    currency = ""
    for r in rows:
        if first(r, "non_chargeable").lower() == "true":
            continue  # informational only, not part of the charges
        svc = first(r, "service_name")
        if not svc:
            continue
        plan = first(r, "plan_name")
        metric = first(r, "usage_metric", "metric")
        unit = first(r, "usage_unit", "unit")
        qty = fnum(first(r, "usage_quantity", "quantity"))
        cost = fnum(first(r, "cost", "usage_cost"))
        kw = ibm_line(svc, plan, metric, unit, qty, first(r, "profile"), period_hours)
        currency = first(r, "currency", "currency_code") or currency
        est.add(service=svc, cost=cost, cloud="ibm", resource_id=first(r, "instance_id"),
                description=f"{plan} {metric}".strip(), start=start, end=end, currency=currency, **kw)
    support = fnum(FILE_META.get("support_cost")) + fnum(FILE_META.get("support_overage"))
    if support:
        est.add(category="support", service=f"IBM Cloud Support ({FILE_META.get('support_type') or 'plan'})", cost=support,
                cloud="ibm", start=start, end=end, currency=currency)

# ---------------------------------------------------------------------------
# FOCUS (FinOps Open Cost and Usage Specification) 1.x
# ---------------------------------------------------------------------------


def ingest_focus(rows, est: Estate, headers):
    use_eff = "effective_cost" in headers
    est.cost_basis.add("focus:effective_cost" if use_eff else "focus:billed_cost")
    for r in rows:
        provider = first(r, "provider_name", "service_provider_name", "provider").lower()
        cloud = "aws" if "aws" in provider or "amazon" in provider else (
            "azure" if "microsoft" in provider or "azure" in provider else (
                "gcp" if "google" in provider else ("oci" if "oracle" in provider else (
                    "alibaba" if "alibaba" in provider or "aliyun" in provider else (
                        "ibm" if provider.startswith("ibm") else provider or "unknown")))))
        chg = first(r, "charge_category").lower()
        svc = first(r, "service_name")
        subcat = first(r, "service_subcategory", "service_category")
        desc = first(r, "charge_description")
        usage = first(r, "x_usage_type", "x_sku_description", "x_sku_meter_name", "sku_meter", "x_meter_name")
        res_type = first(r, "resource_type")
        cost = fnum(first(r, "effective_cost")) if use_eff else fnum(first(r, "billed_cost"))
        if use_eff and chg == "purchase":
            cost = 0.0  # amortised into usage via EffectiveCost
        qty = fnum(first(r, "consumed_quantity", "pricing_quantity", "usage_quantity"))
        unit = first(r, "consumed_unit", "pricing_unit", "usage_unit")
        currency = first(r, "billing_currency")
        start = parse_date(first(r, "charge_period_start"))
        end = parse_date(first(r, "charge_period_end"))
        rid = first(r, "resource_id")
        pricing_cat = first(r, "pricing_category").lower()
        details = {}
        for k in ("sku_price_details", "x_sku_details", "x_sku_price_details"):
            raw = first(r, k)
            if raw.startswith("{"):
                try:
                    details.update(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        text = f"{svc} :: {usage} :: {desc} :: {subcat} :: {res_type}"
        if chg == "tax":
            cat = "tax"
        elif chg == "credit":
            cat = "credit"
        elif chg == "adjustment" and cloud != "oci":
            cat = "discount"
        else:
            cat = classify(text)
        kw = dict(category=cat, service=svc, cost=cost, cloud=cloud, resource_id=rid, description=desc or usage,
                  start=start, end=end, currency=currency)
        if cloud == "oci" and cat not in ("tax", "credit"):
            # OCI marks corrections as Adjustment: they amend usage lines, so classify them like usage
            kw.update(oci_line(svc, desc, first(r, "pricing_unit", "usage_unit", "consumed_unit"),
                               fnum(first(r, "pricing_quantity", "usage_quantity", "consumed_quantity"))))
            est.add(**kw)
            continue
        itype = None
        for k, v in details.items():
            if re.search(r"instance.?type|vm.?size|machine.?type|service.?type|^size$", k, re.I) and isinstance(v, str):
                itype = v
        itype = itype or find_instance_type(usage, cloud) or find_instance_type(desc, cloud) or \
            find_instance_type(first(r, "x_sku_meter_name", "sku_meter"), cloud)
        hours_units = re.match(r"^(\d+\s*)?(hours?|hrs?)$", unit.strip().lower() or "x")
        if cat in ("compute", "database", "cache", "search", "messaging", "gpu", "analytics", "nosql") and itype and hours_units:
            spec = decode(itype, cloud if cloud in ("aws", "azure", "gcp") else None)
            hours = to_hours(qty, unit)
            if spec and spec["gpu_count"] and cat == "compute":
                kw["category"] = "gpu"
            kw.update(instance_type=itype, usage_hours=hours,
                      pricing="spot" if pricing_cat == "dynamic" else ("committed" if pricing_cat == "committed" else "on_demand"),
                      engine=engine_of(text) if cat not in ("compute", "gpu") else None,
                      os_=os_of(text) if cat in ("compute", "gpu") else None, multi_az="multi-az" in text.lower())
            if spec:
                kw.update(vcpu_hours=hours * spec["vcpu"], ram_gb_hours=hours * spec["ram_gb"],
                          gpu_hours=hours * spec["gpu_count"], gpu_model=spec["gpu_model"])
        elif cat in ("block_storage", "object_storage", "file_storage", "backup", "archive_storage") and \
                re.search(r"gb|gib|byte|month", unit.lower()) and not re.search(r"request|operation|iops|throughput", text.lower()):
            kw.update(storage_gb_month=to_gb_month(qty, unit, 730), storage_class="standard")
        elif cat in ("egress", "inter_az", "network_other", "cdn", "nat") and re.search(r"gb|gib|byte", unit.lower()):
            kw.update(transfer_gb=to_gb(qty, unit))
        elif cat == "free":
            continue
        est.add(**kw)

# ---------------------------------------------------------------------------
# Service-level summary (two or more columns: service, cost [, description, quantity, unit])
# ---------------------------------------------------------------------------


def ingest_summary(rows, est: Estate, headers, cloud_hint=None):
    est.cost_basis.add("summary")
    svc_key = next((h for h in ("service", "service_name", "product", "description", "meter_category", "name", "item")
                    if h in headers), None)
    cost_keys = [h for h in headers if re.search(r"cost|amount|total|charge|spend|usd|eur", h)]
    if not svc_key or not cost_keys:
        return 0
    n = 0
    for r in rows:
        svc = r.get(svc_key, "")
        if not svc or re.match(r"^(total|grand total|service total|subtotal)", svc.strip().lower()):
            continue
        cost = sum(fnum(r.get(k)) for k in cost_keys if not re.search(r"usage|quantity", k))
        desc = first(r, "description", "detail", "usage_type")
        text = f"{svc} :: {desc}"
        cat = classify(text)
        kw = dict(category=cat, service=svc, cost=cost, cloud=cloud_hint or "unknown", description=desc)
        itype = find_instance_type(desc) or find_instance_type(svc)
        qty = fnum(first(r, "quantity", "usage_quantity", "hours"))
        unit = first(r, "unit", "usage_unit")
        if itype and qty:
            spec = decode(itype, cloud_hint)
            hours = to_hours(qty, unit or "hrs")
            kw.update(instance_type=itype, usage_hours=hours)
            if spec:
                kw.update(vcpu_hours=hours * spec["vcpu"], ram_gb_hours=hours * spec["ram_gb"],
                          gpu_hours=hours * spec["gpu_count"], gpu_model=spec["gpu_model"])
        elif qty and cat in ("block_storage", "object_storage", "file_storage", "backup", "archive_storage"):
            kw.update(storage_gb_month=to_gb_month(qty, unit or "gb-mo", 730))
        elif qty and cat in ("egress", "inter_az", "network_other", "cdn"):
            kw.update(transfer_gb=to_gb(qty, unit or "gb"))
        est.add(**kw)
        n += 1
    return n


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--output", default="estate.json")
    ap.add_argument("--format", default="auto", choices=["auto", "aws", "azure", "gcp", "oci", "ibm", "focus", "summary"])
    ap.add_argument("--cloud", help="cloud hint for summary files: aws|azure|gcp|oci|ibm|alibaba")
    ap.add_argument("--currency", help="override currency code")
    ap.add_argument("--period-hours", type=float, help="override billing period length in hours (730 = one month)")
    a = ap.parse_args(argv)

    est = Estate()
    total_rows = 0
    for path in a.files:
        if not os.path.exists(path):
            print(f"error: {path} not found", file=sys.stderr)
            return 3
        try:
            rows = iter_rows(path)
            peek = next(rows, None)
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile, csv.Error) as e:
            print(f"error: cannot read {path}: {e}. Export the bill as CSV (UTF-8) and retry.", file=sys.stderr)
            return 3
        if peek is None:
            print(f"warning: {path} has no data rows", file=sys.stderr)
            continue
        headers = set(peek.keys())
        fmt = detect(headers) if a.format == "auto" else a.format
        loc = sniff_locale(path)
        LOCALE.update(decimal_comma=loc["decimal_comma"], dayfirst=loc["dayfirst"])
        if loc["decimal_comma"]:
            print(f"{os.path.basename(path)}: decimal commas detected", file=sys.stderr)
        if loc["dayfirst"]:
            print(f"{os.path.basename(path)}: day-first dates detected (DD/MM/YYYY)", file=sys.stderr)
        elif loc["date_order_ambiguous"] and fmt == "azure":
            print(f"{os.path.basename(path)}: date order ambiguous; assuming MM/DD/YYYY (Azure default). "
                  "Use --period-hours if the period comes out wrong.", file=sys.stderr)
        rows = iter_rows(path)
        next(rows, None)

        def chain():
            yield peek
            yield from rows
        counted = []

        def counting(it):
            for x in it:
                counted.append(1)
                yield x
        est.sources.append({"file": os.path.basename(path), "format": fmt})
        est.kinds.add(fmt)
        if fmt == "aws":
            ingest_aws(counting(chain()), est, headers)
        elif fmt == "azure":
            ingest_azure(counting(chain()), est, headers)
        elif fmt == "gcp":
            ingest_gcp(counting(chain()), est, headers)
        elif fmt == "ibm":
            ingest_ibm(counting(chain()), est, headers)
        elif fmt == "oci":
            ingest_oci(counting(chain()), est, headers)
        elif fmt == "focus":
            ingest_focus(counting(chain()), est, headers)
        else:
            ingest_summary(counting(chain()), est, headers, a.cloud)
        total_rows += len(counted)
        print(f"{os.path.basename(path)}: format={fmt}, rows={len(counted)}", file=sys.stderr)

    if not est.items:
        print("error: no usable billing rows found. Check the export type (see references/reading-bills.md).",
              file=sys.stderr)
        return 2
    out = est.finish(a.period_hours, a.currency)
    with open(a.output, "w") as f:
        json.dump(out, f, indent=1)
    m = out["meta"]
    if len([c for c, v in m["currencies_seen"].items() if v > 0]) > 1:
        print(f"warning: several currencies in the input {m['currencies_seen']}; all lines are treated as {m['currency']}. "
              "Run each currency separately.", file=sys.stderr)
    print(json.dumps({
        "output": a.output, "rows": total_rows, "items": len(out["items"]), "currency": m["currency"],
        "period": [m["period_start"], m["period_end"]], "months": m["months_covered"],
        "total_ex_tax_ex_credits": m["total_cost_ex_tax_ex_credits"], "credits": m["credits"],
        "cost_by_category": dict(list(m["cost_by_category"].items())[:12]),
        "unclassified_cost": round(sum(x["cost"] for x in m["unclassified_top"]), 2),
    }, indent=1))
    if m["months_covered"] < 0.25:
        print("warning: the input covers less than a week; estimates need at least one full month.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
