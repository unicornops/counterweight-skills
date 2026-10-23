#!/usr/bin/env python3
"""Render a Counterweight plan.json as a client-ready report (Markdown and/or branded HTML).

Usage:
  python3 report.py plan.json -o report.md [--html report.html] [--client "Acme Ltd"]
                    [--sector financial|public|other] [--provider hetzner] [--prepared-for-email a@b.c]

The Markdown is portable (any AI tool, email, docs). The HTML is one self-contained
page in the Counterweight brand (Paper theme), with its fonts embedded, suitable to publish or print to PDF.
Pure standard library.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(HERE, "..", "assets", "counterweight-lockup-ink.svg")
FONTS = os.path.join(HERE, "..", "assets", "fonts")
# (family, file, weight range): embedded in report.html so it needs no font service and works offline.
FONT_FACES = [
    ("Archivo", "archivo-latin-wdth-normal.woff2", "100 900"),
    ("IBM Plex Mono", "ibm-plex-mono-latin-400-normal.woff2", "400"),
    ("IBM Plex Mono", "ibm-plex-mono-latin-500-normal.woff2", "500"),
]

CAT_LABEL = {
    "compute": "Virtual machines", "container": "Containers / app platforms", "serverless": "Serverless functions",
    "gpu": "GPU instances", "kubernetes_control": "Managed Kubernetes control plane", "database": "Managed databases",
    "database_storage": "Database storage", "cache": "Managed caches", "search": "Managed search",
    "search_storage": "Search storage", "messaging": "Queues and streaming", "block_storage": "Block storage (disks)",
    "file_storage": "File storage", "object_storage": "Object storage", "archive_storage": "Archive storage",
    "backup": "Snapshots and backups", "egress": "Internet egress", "inter_az": "Cross-zone traffic",
    "nat": "NAT gateways", "ip": "Public IP addresses", "load_balancer": "Load balancers",
    "network_other": "Private links, VPN, peering", "cdn": "CDN", "dns": "DNS", "monitoring": "Monitoring and logging",
    "security": "Security services", "identity": "Identity", "email": "Email and messaging APIs",
    "marketplace": "Marketplace", "analytics": "Analytics / data warehouse", "nosql": "Proprietary NoSQL",
    "ml_managed": "Managed AI/ML services", "support": "Cloud support plan", "commitment": "Unused commitments",
    "discount": "Negotiated discounts", "credit": "Credits", "tax": "Tax", "other": "Other / unclassified",
    "cache_storage": "Cache storage",
}
DISP_LABEL = {"migrate": "Moves to bare metal", "retain": "Stays as is", "eliminate": "Goes away",
              "baseline_only": "Discount (in your bill today)", "exclude": "Excluded"}
SCEN_LABEL = {"like_for_like": "Like-for-like", "right_sized": "Right-sized"}
KIND_LABEL = {"aws": "an AWS Cost and Usage Report", "azure": "an Azure cost export", "gcp": "a Google Cloud billing export",
              "focus": "a FOCUS cost export", "summary": "an invoice summary", "invoice_pdf": "a PDF invoice",
              "manual": "figures read from your invoice"}
ENGINE_LABEL = {"postgres": "PostgreSQL", "mysql": "MySQL", "sqlserver": "SQL Server", "oracle": "Oracle", "redis": "Redis",
                "memcached": "Memcached", "opensearch": "OpenSearch", "kafka": "Kafka", "rabbitmq": "RabbitMQ",
                "activemq": "ActiveMQ", "database": "databases", "cache": "caches", "search": "search", "messaging": "brokers"}


def dmy(d, fmt="dmy"):
    """Day month year without platform-specific strftime flags (%-d fails on Windows)."""
    parts = {"d": str(d.day), "m": d.strftime("%B"), "y": str(d.year)}
    return " ".join(parts[c] for c in fmt)


def nice_date(s):
    try:
        return dmy(dt.date.fromisoformat(s))
    except (TypeError, ValueError):
        return s or "unknown"


def nice_period(start, end):
    try:
        a = dt.date.fromisoformat(start)
        b = dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        return "one billing period"
    if b > a and b.day == 1:
        b = b - dt.timedelta(days=1)  # exports give an exclusive end date
    fa = dmy(a) if a.year != b.year else (dmy(a, "dm") if a.month != b.month else dmy(a, "d"))
    return f"{fa} to {dmy(b)}"


def eur(x, k=False):
    if x is None:
        return "n/a"
    neg = x < 0
    x = abs(x)
    if k and x >= 1_000_000:
        s = f"€{x/1_000_000:.2f}M".replace(".00M", "M")
    elif k and x >= 100_000:
        s = f"€{x/1000:.0f}k"
    elif k and x >= 10_000:
        s = f"€{x/1000:.1f}k".replace(".0k", "k")
    elif k and x >= 1000:
        s = f"€{x/1000:.1f}k".replace(".0k", "k")
    else:
        s = f"€{x:,.0f}"
    return ("−" if neg else "") + s


def rng(a, b, k=True, suffix=""):
    """'€16k–€18k' or a single figure when both ends round the same."""
    lo, hi = sorted([a, b])
    x, y = eur(lo, k), eur(hi, k)
    return f"{x}{suffix}" if x == y else f"{x}–{y}{suffix}"


def pct(x):
    return "n/a" if x is None else f"{x:.0f}%"


# ---------------------------------------------------------------------------
# Content model: a list of blocks, rendered to Markdown and HTML
# ---------------------------------------------------------------------------

def build(plan, client, sector, provider_key):
    b = []
    P = plan["providers"]
    pk = provider_key or plan["recommended_provider"]
    prov = P[pk]
    like = prov["scenarios"]["like_for_like"]
    right = prov["scenarios"]["right_sized"]
    base = plan["baseline_eur_month"]
    inp = plan["input"]
    period = nice_period(inp.get("period_start"), inp.get("period_end"))
    today = dmy(dt.date.today())

    b.append(("eyebrow", "Price your exit"))
    b.append(("h1", f"Indicative bare-metal estimate{(' for ' + client) if client else ''}"))
    kinds = " and ".join(KIND_LABEL.get(k, k) for k in (inp.get("input_kind") or [])) or "a cloud bill"
    b.append(("meta", f"Prepared {today} from {kinds} covering {period}. "
                      f"Prices exclude VAT. Bare-metal prices as of {nice_date(plan.get('catalogue_as_of'))}."))

    # ---- headline
    lo = min(like["saving_eur_month"], right["saving_eur_month"])
    hi = max(like["saving_eur_month"], right["saving_eur_month"])
    b.append(("beam",))
    b.append(("h2", "The short answer"))
    b.append(("stats", [
        ("Your cloud bill, average month", eur(base, True) + "/mo", "ink"),
        (f"On {prov['name']} bare metal", rng(right['monthly_eur']['total'], like['monthly_eur']['total'], suffix="/mo"), "ink"),
        ("Indicative saving", rng(max(lo, 0), hi, suffix="/mo") if hi > 0 else "No saving at this size", "brass" if hi > 0 else "risk"),
    ]))
    if hi > 0:
        lead = (f"Moving the workloads that suit bare metal to {prov['name']} would cost about "
                f"{rng(right['monthly_eur']['total'], like['monthly_eur']['total'])} a month, against "
                f"{eur(base, True)} today: {pct(min(like['saving_pct'], right['saving_pct']))} to "
                f"{pct(max(like['saving_pct'], right['saving_pct']))} less, or {eur(lo * 12, True)} to {eur(hi * 12, True)} a year. ")
    else:
        lead = (f"At this size the fixed costs of running your own servers (redundancy, operations effort) outweigh "
                f"what you would save: bare metal on {prov['name']} would cost about "
                f"{rng(right['monthly_eur']['total'], like['monthly_eur']['total'])} a month against {eur(base, True)} today. ")
    o_lo, o_hi = sorted([right["monthly_eur"]["operations_effort"], like["monthly_eur"]["operations_effort"]])
    f_lo, f_hi = sorted([right["ops_fte"], like["ops_fte"]])
    ops_txt = f"{f_lo:.2f} of an engineer, {eur(o_lo, True)}/mo" if abs(o_hi - o_lo) < 1 else \
        f"{f_lo:.2f}–{f_hi:.2f} of an engineer, {eur(o_lo, True)}–{eur(o_hi, True)}/mo"
    ex_lo, ex_hi = sorted([like["saving_excluding_operations_pct"], right["saving_excluding_operations_pct"]])
    if hi <= 0:
        lead += ("That includes the minimum redundant cluster and a quarter of an engineer's time; a cloud bill this size "
                 "is usually better reduced by tidying up and renegotiating where you are.")
    else:
        lead += ("Both figures include an allowance for the extra operations work "
                 f"({ops_txt}) "
                 "and keep the services that are better left where they are. "
                 f"Without the operations allowance the saving is {pct(ex_lo)} to {pct(ex_hi)}; with a full extra engineer "
                 f"({eur(plan['assumptions']['ops_fte_cost_eur'])} a year) it would be "
                 f"{eur(min(like['saving_with_one_fte_eur_month'], right['saving_with_one_fte_eur_month']), True)} to "
                 f"{eur(max(like['saving_with_one_fte_eur_month'], right['saving_with_one_fte_eur_month']), True)} a month.")
    b.append(("lead", lead))
    b.append(("p", f"Like-for-like ({eur(like['monthly_eur']['total'], True)}/mo) buys one modern server thread for every "
                   f"cloud vCPU, the same memory and the disk you provision today. Right-sized "
                   f"({eur(right['monthly_eur']['total'], True)}/mo) assumes typical utilisation, newer CPUs and the disk "
                   f"you actually use, which a Teardown measures rather than assumes. "
                   f"Your bill for the period was {eur(plan.get('invoice_total_eur_period', base))} before tax and credits; "
                   f"figures here are per average month. Confidence: **{plan['confidence']['grade']}** "
                   f"({plan['confidence']['share_of_spend_sized'] * 100:.0f}% of spend sized, "
                   f"{plan['confidence']['share_of_sized_spend_from_usage_data'] * 100:.0f}% of that from usage data rather than cost alone)."))
    b.append(("chart", [("Cloud today", base, "graphite"),
                        (f"{prov['name']} like-for-like", like["monthly_eur"]["total"], "brass"),
                        (f"{prov['name']} right-sized", right["monthly_eur"]["total"], "brass")]))

    # ---- what we read
    b.append(("beam",))
    b.append(("h2", "What we read"))
    rows = []
    for cat, v in plan["baseline_by_category_eur_month"].items():
        if abs(v["cost"]) < 0.5 or v["disposition"] == "exclude":
            continue
        rows.append([CAT_LABEL.get(cat, cat), eur(v["cost"]), DISP_LABEL.get(v["disposition"], v["disposition"])])
    rows.append(["**Total (excl. VAT and credits)**", f"**{eur(base)}**", ""])
    b.append(("table", ["Line of your bill", "Per month", "In the estimate"], rows, [False, True, False]))

    # ---- estate
    d = plan["demand"]
    b.append(("beam",))
    b.append(("h2", "Your estate in numbers"))
    ap, dp = d["app_pool"], d["data_pool"]
    items = [
        f"Application capacity: about {ap['like_t']:.0f} vCPU-equivalents and {ap['like_r']:.0f} GB of memory provisioned on average"
        + (f", across {d['kubernetes_clusters']} managed Kubernetes cluster(s)" if d.get("kubernetes_clusters") else "") + ".",
    ]
    if dp["like_t"] or dp["nvme_gb"]:
        engines = ", ".join(ENGINE_LABEL.get(e, e) for e in d["data_engines"]) or "databases"
        items.append(f"Data services ({engines}): {dp['like_t']:.0f} vCPU-equivalents and "
                     f"{dp['like_r']:.0f} GB of memory including replicas, with {dp['nvme_gb'] / 1024:.1f} TB of fast local storage.")
    st = d["storage_gb"]
    if st["block_gb"] or st["file_gb"]:
        items.append(f"Disks and shared file storage: {(st['block_gb'] + st['file_gb']) / 1024:.1f} TB.")
    if st["object_gb"]:
        items.append(f"Object storage: {st['object_gb'] / 1024:.1f} TB.")
    if d["egress_gb_month"]:
        items.append(f"Internet egress: {d['egress_gb_month'] / 1024:.1f} TB a month (about {d['egress_avg_mbps']:.0f} Mbit/s on average).")
    for model, g in d.get("gpu", {}).items():
        per = f", up to {g['per_instance']:g} per instance" if g.get("per_instance", 0) > 1 else ""
        items.append(f"GPUs: {g['avg_gpus']:.1f} × {model} on average{per}.")
    b.append(("ul", items))

    # ---- proposed shape
    def shape_rows(sc):
        rows = []
        for s in sc["servers"]:
            spec = f"{s['threads_each']} threads, {s['ram_gb_each']} GB RAM, {s['nvme_gb_each'] / 1000:.2f} TB NVMe"
            if s.get("gpu"):
                spec += f", {s['gpu'].get('count', 1)} × {s['gpu'].get('model')}"
            pool = s["pool"]
            pool = "GPU" + pool[3:] if pool.startswith("gpu") else pool[:1].upper() + pool[1:]
            rows.append([pool, f"{s['count']} × {s['model']}", spec, eur(s["monthly_eur"])])
        return rows

    b.append(("beam",))
    b.append(("h2", f"Proposed bare-metal shape on {prov['name']}"))
    gpu_note = " GPU servers have no spare." if any(x["pool"].startswith("gpu") for x in right["servers"]) else ""
    b.append(("p", f"Right-sized ({right['layout']}, {right['server_count']} servers). The application pool keeps a spare "
                   "server and every database runs a replica on a second server, so one failed machine does not take a "
                   f"service down.{gpu_note} Everything sits in one provider region, spread across its data-centre "
                   "buildings where the provider allows, with backups in a second location. A warm standby site for "
                   "disaster recovery is not priced."))
    b.append(("table", ["Pool", "Servers", "Each server", "Per month"], shape_rows(right), [False, False, False, True]))
    b.append(("p", f"Like-for-like ({like['layout']}, {like['server_count']} servers):"))
    b.append(("table", ["Pool", "Servers", "Each server", "Per month"], shape_rows(like), [False, False, False, True]))
    if right.get("gpu_unmatched"):
        b.append(("p", f"No server in the catalogue matches {', '.join(right['gpu_unmatched'])} GPUs, so those stay where they are in this estimate."))

    # ---- monthly comparison
    b.append(("beam",))
    b.append(("h2", "Month by month"))
    lines = [("Servers", "servers"), ("Object storage", "object_storage"), ("Off-site backup storage", "backup_storage"),
             ("Traffic above allowance", "traffic_overage"), ("10 Gbit uplinks", "uplinks"), ("Load balancers", "load_balancers"),
             ("Contingency", "contingency"), ("Cloud services kept", "retained_cloud_services"),
             ("Operations effort", "operations_effort")]
    rows = []
    for lab, key in lines:
        a, c = like["monthly_eur"].get(key, 0), right["monthly_eur"].get(key, 0)
        if not a and not c:
            continue
        rows.append([lab, eur(a), eur(c)])
    rows.append(["**Bare metal total**", f"**{eur(like['monthly_eur']['total'])}**", f"**{eur(right['monthly_eur']['total'])}**"])
    rows.append(["Cloud today", eur(base), eur(base)])
    def sv(sc):
        if sc["saving_eur_month"] < 0:
            return f"**None: {eur(-sc['saving_eur_month'])} more**"
        return f"**{eur(sc['saving_eur_month'])} ({pct(sc['saving_pct'])})**"
    rows.append(["**Saving**", sv(like), sv(right)])
    b.append(("table", ["", "Like-for-like", "Right-sized"], rows, [False, True, True]))

    # ---- providers
    if len(P) > 1:
        b.append(("h3", "Other providers"))
        rows = []
        for k, r in sorted(P.items(), key=lambda kv: kv[1]["scenarios"]["right_sized"]["monthly_eur"]["total"]):
            rs, lf = r["scenarios"]["right_sized"], r["scenarios"]["like_for_like"]
            locs = r.get("locations") or []
            loc_txt = ", ".join(locs[:3]) + (f" and {len(locs) - 3} more" if len(locs) > 3 else "")
            rows.append([r["name"] + (" (used above)" if k == pk else ""), loc_txt,
                         eur(lf["monthly_eur"]["total"]), eur(rs["monthly_eur"]["total"]),
                         pct(rs["saving_pct"]) if rs["saving_pct"] and rs["saving_pct"] > 0 else "none"])
        b.append(("table", ["Provider", "Locations", "Like-for-like", "Right-sized", "Saving (right-sized)"], rows,
                  [False, False, True, True, True]))
        notes = [f"{r['name']}: {r['pricing_caveat']}" for k, r in P.items() if r.get("pricing_caveat")]
        if notes:
            b.append(("ul", notes))

    # ---- one-offs
    b.append(("beam",))
    b.append(("h2", "One-off costs and timing"))
    oo = right["one_off_eur"]
    items = [f"Server setup fees: {eur(oo['setup_fees'])}.",
             f"Running both estates side by side for {plan['assumptions']['parallel_run_months']} months while you move: "
             f"about {eur(oo['parallel_running'])}.",
             f"Those two costs are recovered in about {right['payback_months']} months of right-sized savings." if right.get("payback_months") else
             "At this size the savings do not recover the setup and parallel-running costs."]
    if plan.get("exit_data_tb"):
        items.append(f"Moving your {plan['exit_data_tb']:.0f} TB of data out would cost about {eur(plan['exit_transfer_list_price_eur'])} "
                     "at list egress prices. AWS, Microsoft and Google have offered free data transfer out to customers who "
                     "are leaving since 2024, on request and with conditions (AWS, for example, approves each request and "
                     "expects the move to finish within 60 days). From 12 January 2027 the EU Data Act bans switching "
                     "charges, though providers may still charge egress at cost while you run both estates in parallel.")
    items.append("Not included: engineering effort for the move (costed in an Exit Plan), any remaining Reserved "
                 "Instance or Savings Plan terms, and shortfalls against committed-spend agreements (EDP, MACC, "
                 "committed use). Check those before setting a date.")
    b.append(("ul", items))

    # ---- stays
    if plan.get("retained_by_category_eur_month"):
        b.append(("beam",))
        b.append(("h2", "What stays where it is"))
        rows = [[CAT_LABEL.get(k, k), eur(v)] for k, v in plan["retained_by_category_eur_month"].items() if v >= 0.5]
        b.append(("p", "These services are kept at today's cost in the estimate. Some are cheap where they are (DNS, archive "
                       "storage), some are proprietary and need re-engineering before they can move (data warehouses, "
                       "proprietary NoSQL), and some you would replace with another SaaS product."))
        b.append(("table", ["Service", "Per month"], rows, [False, True]))

    # ---- assumptions & risks
    A = plan["assumptions"]
    b.append(("beam",))
    b.append(("h2", "What this estimate assumes"))
    b.append(("ul", [
        f"Servers are planned to run at no more than {A['cpu_target'] * 100:.0f}% CPU and {A['ram_target'] * 100:.0f}% memory, "
        f"with one spare server in the application pool and every database on a primary and a replica.",
        f"Cloud vCPUs on Arm or without hyper-threading count as {1.4:.1f} hardware threads; burstable instances count at their baseline.",
        f"Right-sized assumes {A['cpu_util'] * 100:.0f}% of today's CPU and {A['ram_util'] * 100:.0f}% of memory is needed, "
        "adjusted for newer CPUs. A Teardown replaces this with your measured utilisation.",
        f"Disks become software-replicated volumes ({A['block_replication']} copies on separate servers) with "
        f"{A['growth_headroom'] * 100:.0f}% headroom for growth; right-sized assumes {A['block_used_fraction'] * 100:.0f}% of "
        f"provisioned disk is in use. Databases run on local NVMe with a replica on a second server. Off-site backups hold "
        f"{A['backup_factor']:.0f}× your primary data.",
        f"Operations effort is the extra engineering time for running your own servers, Kubernetes, databases and "
        f"storage, scaled to the size of the fleet (at least a quarter of an engineer) and priced at "
        f"{eur(A['ops_fte_cost_eur'])} a year per engineer, fully loaded. If your team already runs servers or "
        "Kubernetes, much of this effort exists today.",
        f"{A['contingency'] * 100:.0f}% contingency on bare-metal costs. Setup fees are spread over {A['horizon_months']} months "
        "when comparing server models.",
        f"US dollar bills are converted at 1 USD = {A['fx_usd_eur']} EUR." if (inp.get("currency") or "").upper() == "USD" else
        f"Bill currency: {inp.get('currency')}.",
    ]))
    flags = [f["text"] for f in plan.get("flags", []) if f.get("provider") in (None, pk)]
    if sector == "financial":
        flags.append("DORA Art. 28(8) requires financial entities to have exit strategies for ICT services supporting "
                     "critical or important functions, and Delegated Regulation (EU) 2024/1773 sets what the contracts "
                     "must cover. A costed bare-metal option is evidence for that plan, not the plan itself.")
    if flags:
        b.append(("h3", "What could move the number"))
        b.append(("ul", flags))

    # ---- next step
    b.append(("beam",))
    b.append(("eyebrow", "Teardown"))
    b.append(("h2", "Next step: book a Teardown"))
    b.append(("p", "This estimate reads your bill. A Teardown reads your estate: two weeks, read-only access, measured "
                   "utilisation instead of assumptions, and a costed plan you can take to procurement whether or not you "
                   "hire us to carry it out. If your renewal is coming up, price your exit before you sign."))
    b.append(("cta", "Book a Teardown", "mailto:hello@counterweight.ie?subject=Teardown"))
    b.append(("meta", "Counterweight is a trading name of Unicorn Operations Limited, registered in Ireland, company "
                      "number 774973, registered office Mullen, Frenchpark, Ireland. counterweight.ie · hello@counterweight.ie. "
                      "This is an indicative estimate produced from billing data alone. It is not a quote or an offer; "
                      "provider prices change and must be confirmed at order."))
    return b


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def to_md(blocks):
    out = []
    for blk in blocks:
        k = blk[0]
        if k == "eyebrow":
            out.append(f"**{blk[1].upper()}**")
        elif k == "h1":
            out.append(f"# {blk[1]}")
        elif k == "h2":
            out.append(f"## {blk[1]}")
        elif k == "h3":
            out.append(f"### {blk[1]}")
        elif k in ("p", "lead"):
            out.append(blk[1])
        elif k == "meta":
            out.append(f"*{blk[1]}*")
        elif k == "beam":
            out.append("---")
        elif k == "stats":
            out.append("\n".join(["| " + " | ".join(s[0] for s in blk[1]) + " |",
                                   "|" + "---|" * len(blk[1]),
                                   "| " + " | ".join(f"**{s[1]}**" for s in blk[1]) + " |"]))
        elif k == "ul":
            out.append("\n".join(f"- {x}" for x in blk[1]))
        elif k == "table":
            head, rows, right = blk[1], blk[2], blk[3]
            t = ["| " + " | ".join(head) + " |", "|" + "|".join("---:" if r else "---" for r in right) + "|"]
            t += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
            out.append("\n".join(t))
        elif k == "chart":
            mx = max(v for _, v, _ in blk[1]) or 1
            lines = ["```"]
            for lab, v, _ in blk[1]:
                bar = "█" * max(1, round(28 * v / mx))
                lines.append(f"{lab[:28]:<28} {bar} {eur(v, True)}/mo")
            lines.append("```")
            out.append("\n".join(lines))
        elif k == "cta":
            out.append(f"**[{blk[1]}]({blk[2]})**")
    return "\n\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTML (Counterweight Paper theme)
# ---------------------------------------------------------------------------

CSS = """
:root{--surface:#f3f0e9;--surface-raised:#fbf9f4;--surface-sunken:#e7e3d9;--ink:#15171a;--ink-muted:#4a5059;
--ink-faint:#5c636d;--hairline:#cfc9bc;--rule:#7a8089;--graphite:#262a30;--on-graphite:#f1eee7;--steel:#6a727c;
--brass:#b8862f;--on-brass:#15171a;--brass-mark:#a3761f;--brass-ink:#7a5512;--risk:#a33a24;--focus:#7a5512;
--sans:"Archivo","Helvetica Neue",Arial,sans-serif;--mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--surface);color:var(--ink);font:400 16px/26px var(--sans)}
main{max-width:880px;margin:0 auto;padding:48px 24px 96px}
@media (max-width:640px){main{padding:24px 16px 64px}}
.logo{display:block;height:40px;width:auto;margin-bottom:48px}
.eyebrow{font:600 12px/16px var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-muted);margin:32px 0 8px}
h1{font:700 40px/44px var(--sans);letter-spacing:-.01em;margin:0 0 16px}
h2{font:650 30px/36px var(--sans);letter-spacing:-.005em;margin:32px 0 16px}
h3{font:650 22px/28px var(--sans);margin:32px 0 12px}
@media (max-width:640px){h1{font-size:30px;line-height:36px}h2{font-size:24px;line-height:30px}}
p{margin:0 0 16px;max-width:70ch}.lead{font-size:20px;line-height:30px}
.meta{font-size:14px;line-height:20px;color:var(--ink-faint);max-width:70ch}
hr.beam{border:0;border-top:4px solid var(--ink);margin:64px 0 0}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:0;border:1px solid var(--hairline);background:var(--surface-raised);margin:24px 0}
.stat{padding:24px;border-right:1px solid var(--hairline)}.stat:last-child{border-right:0}
.stat .l{font-size:14px;line-height:20px;color:var(--ink-muted)}
.stat .v{font:500 clamp(20px,2.3vw,28px)/1.15 var(--mono);margin-top:8px;font-variant-numeric:tabular-nums}
.stat.brass{background:var(--brass)}.stat.brass .l,.stat.brass .v{color:var(--on-brass)}
.stat.risk .v{color:var(--risk)}
@media (max-width:640px){.stats{grid-template-columns:1fr}.stat{border-right:0;border-bottom:1px solid var(--hairline)}}
.tw{overflow-x:auto;margin:16px 0 24px;border:1px solid var(--hairline);background:var(--surface-raised)}
table{border-collapse:collapse;width:100%;font-size:14px;line-height:20px}
th{background:var(--surface-sunken);text-align:left;font-weight:600;color:var(--ink-muted)}
th,td{padding:12px;border-bottom:1px solid var(--hairline);vertical-align:top}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;white-space:nowrap}td.n{font-family:var(--mono);font-variant-numeric:tabular-nums}
td.n b{font-weight:500}
.save{color:var(--brass-ink)}
ul{padding-left:20px;margin:0 0 16px;max-width:70ch}li{margin-bottom:8px}
.fig{font-family:var(--mono);font-weight:500}
.chart{margin:24px 0}.chart .row{display:grid;grid-template-columns:200px 1fr 96px;align-items:center;gap:12px;margin:8px 0;font-size:14px}
.chart .bar{height:20px}.chart .bar.graphite{background:var(--graphite)}.chart .bar.brass{background:var(--brass-mark)}
.chart .val{font-family:var(--mono);text-align:right}
@media (max-width:640px){.chart .row{grid-template-columns:1fr 72px}.chart .lab{grid-column:1/3}}
a.cta{display:inline-block;border:1px solid var(--rule);color:var(--ink);text-decoration:none;font:600 16px/24px var(--sans);
padding:12px 24px;border-radius:2px;margin:8px 0 32px}
a.cta:hover{background:var(--surface-sunken)}a:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media print{body{background:#fff}hr.beam{margin-top:32px}main{padding:0}}
"""


def md_inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(−?€[\d,.]+[kM]?(?:/mo)?|\d+(?:\.\d+)?%)", r'<span class="fig">\1</span>', s)
    return s


def font_faces():
    """@font-face rules with the bundled fonts inlined as data: URIs (SIL OFL 1.1, see assets/fonts)."""
    rules = []
    for family, name, weight in FONT_FACES:
        path = os.path.join(FONTS, name)
        if not os.path.exists(path):
            continue  # the CSS font stacks fall back to system fonts
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        rules.append(f'@font-face{{font-family:"{family}";src:url(data:font/woff2;base64,{data}) format("woff2");'
                     f"font-weight:{weight};font-style:normal;font-display:swap}}")
    return "".join(rules)


def to_html(blocks, title):
    logo = ""
    if os.path.exists(LOGO):
        with open(LOGO) as f:
            svg = f.read()
        svg = svg.replace("<svg ", '<svg class="logo" ', 1)
        logo = svg
    parts = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
             f"<title>{html.escape(title)}</title>"
             f"<style>{font_faces()}{CSS}</style></head><body><main>{logo}"]
    for blk in blocks:
        k = blk[0]
        if k == "eyebrow":
            parts.append(f'<div class="eyebrow">{html.escape(blk[1])}</div>')
        elif k in ("h1", "h2", "h3"):
            parts.append(f"<{k}>{html.escape(blk[1])}</{k}>")
        elif k == "lead":
            parts.append(f'<p class="lead">{md_inline(blk[1])}</p>')
        elif k == "p":
            parts.append(f"<p>{md_inline(blk[1])}</p>")
        elif k == "meta":
            parts.append(f'<p class="meta">{html.escape(blk[1])}</p>')
        elif k == "beam":
            parts.append('<hr class="beam">')
        elif k == "stats":
            cells = "".join(f'<div class="stat {c}"><div class="l">{html.escape(l)}</div><div class="v">{html.escape(v)}</div></div>'
                            for l, v, c in blk[1])
            parts.append(f'<div class="stats">{cells}</div>')
        elif k == "ul":
            parts.append("<ul>" + "".join(f"<li>{md_inline(x)}</li>" for x in blk[1]) + "</ul>")
        elif k == "table":
            head, rows, right = blk[1], blk[2], blk[3]
            th = "".join(f'<th class="{"n" if r else ""}">{html.escape(h)}</th>' for h, r in zip(head, right))
            trs = []
            for row in rows:
                tds = []
                for c, r in zip(row, right):
                    cell = md_inline(str(c))
                    if r and "Saving" in str(row[0]):
                        cell = f'<span class="save">{cell}</span>'
                    tds.append(f'<td class="{"n" if r else ""}">{cell}</td>')
                trs.append("<tr>" + "".join(tds) + "</tr>")
            parts.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')
        elif k == "chart":
            mx = max(v for _, v, _ in blk[1]) or 1
            rows = "".join(f'<div class="row"><div class="lab">{html.escape(l)}</div><div class="bar {c}" style="width:{max(1, 100 * v / mx):.1f}%"></div>'
                           f'<div class="val">{eur(v, True)}/mo</div></div>' for l, v, c in blk[1])
            parts.append(f'<div class="chart" role="img" aria-label="Monthly cost: cloud today compared with bare metal">{rows}</div>')
        elif k == "cta":
            parts.append(f'<a class="cta" href="{html.escape(blk[2])}">{html.escape(blk[1])}</a>')
    parts.append("</main></body></html>")
    return "".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("-o", "--output", default="report.md")
    ap.add_argument("--html", help="also write a branded HTML page here")
    ap.add_argument("--client", default="")
    ap.add_argument("--sector", default="other", choices=["financial", "public", "other"])
    ap.add_argument("--provider", help="catalogue key to feature (default: recommended)")
    a = ap.parse_args(argv)
    with open(a.plan) as f:
        plan = json.load(f)
    if a.provider and a.provider not in plan["providers"]:
        sys.exit(f"provider {a.provider} not in plan; choose from {', '.join(plan['providers'])}")
    blocks = build(plan, a.client, a.sector, a.provider)
    with open(a.output, "w") as f:
        f.write(to_md(blocks))
    if a.html:
        with open(a.html, "w") as f:
            f.write(to_html(blocks, f"Bare-metal estimate{(' · ' + a.client) if a.client else ''}"))
    print(json.dumps({"markdown": a.output, "html": a.html}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
