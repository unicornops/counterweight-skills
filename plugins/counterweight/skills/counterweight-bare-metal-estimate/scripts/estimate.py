#!/usr/bin/env python3
"""Size a bare-metal estate from a Counterweight estate file and price it per provider.

Usage:
  python3 estimate.py estate.json -o plan.json [--catalogue ../assets/catalogue.json]
        [--fx-usd-eur 0.86] [--providers hetzner,ovhcloud,leaseweb]
        [--cpu-util 0.6] [--ram-util 0.9] [--ops-fte 0.5] [--ops-fte-cost 110000]
        [--assumptions assumptions.json]

What it does, in order:
  1. Converts each estate item to monthly cost (EUR) and average concurrent demand
     (SMT-thread equivalents, GB RAM, GPUs, GB stored, GB transferred).
  2. Applies a disposition per item: migrate | retain | eliminate | exclude.
     Defaults are by category (see DEFAULT_DISPOSITION); override per item in
     assumptions.json -> "dispositions": {"<category>" or "<service>": "retain"}.
  3. Groups migrating demand into pools (app, data, gpu, storage), applies
     headroom and redundancy, and picks the cheapest server model per pool per
     provider from the catalogue (monthly price + setup amortised over the horizon).
  4. Prices storage, backup, traffic, retained cloud services, an operations
     effort line and contingency, for two scenarios:
       like_for_like : one bare-metal thread per cloud vCPU, GB for GB of RAM
       right_sized   : applies utilisation and per-thread generation factors
  5. Writes plan.json for report.py. Prints a one-screen summary to stderr.

Pure standard library. All numbers are indicative planning estimates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from specs import decode, GEN_FACTOR, NO_SMT_THREAD_EQUIV, GPU_MEM  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CATALOGUE = os.path.join(HERE, "..", "assets", "catalogue.json")

DEFAULT_DISPOSITION = {
    # sized onto bare metal
    "compute": "migrate", "container": "migrate", "serverless": "migrate", "gpu": "migrate",
    "kubernetes_control": "migrate", "database": "migrate", "database_storage": "migrate",
    "cache": "migrate", "cache_storage": "migrate", "search": "migrate", "search_storage": "migrate",
    "messaging": "migrate", "block_storage": "migrate", "file_storage": "migrate",
    "object_storage": "migrate", "backup": "migrate", "egress": "migrate", "load_balancer": "migrate",
    "monitoring": "migrate",
    # disappear on bare metal (private networking is free or flat-rate; support replaced by ops line)
    "inter_az": "eliminate", "nat": "eliminate", "ip": "eliminate", "support": "eliminate",
    "commitment": "eliminate",   # RI/SP fees: the covered usage is already counted as demand
    # kept as they are (SaaS-like, proprietary, or cheaper where they are)
    "network_other": "retain", "security": "retain", "identity": "retain", "email": "retain", "dns": "retain",
    "cdn": "retain", "marketplace": "retain", "licence": "retain", "analytics": "retain", "nosql": "retain",
    "ml_managed": "retain", "archive_storage": "retain", "other": "retain",
    # in the baseline but not a thing to size
    "discount": "baseline_only",
    # out of the comparison entirely
    "credit": "exclude", "tax": "exclude", "free": "exclude",
}

POOL_OF = {
    "compute": "app", "container": "app", "serverless": "app", "kubernetes_control": "app", "monitoring": "app",
    "database": "data", "cache": "data", "search": "data", "messaging": "data",
    "gpu": "gpu",
}

DEFAULTS = {
    "fx_usd_eur": None,          # taken from catalogue if not given
    "fx_gbp_eur": None,
    "horizon_months": 36,        # setup fees amortised over this
    "cpu_target": 0.70,          # max planned thread utilisation per server
    "ram_target": 0.85,          # max planned RAM allocation per server (after OS reserve)
    "os_reserve_gb": 4,
    "nvme_fill": 0.75,
    "app_spare_nodes": 1,        # N+1 in the app pool
    "app_min_nodes": 3,          # quorum for a cluster scheduler
    "data_min_nodes": 2,
    "db_replicas": 2,            # primary + replica on separate servers
    "block_replication": 2,      # replicas for disks that become software-replicated volumes (Longhorn/Ceph size=2)
    "block_used_fraction": 0.7,  # right-sized: share of provisioned cloud disk actually used
    "storage_min_nodes": 3,
    "growth_headroom": 0.25,     # extra capacity for data growth
    "cpu_util": 0.60,            # right-sized: share of like-for-like CPU actually needed (p95 based)
    "ram_util": 0.90,            # right-sized: share of like-for-like RAM actually needed
    "burst_floor_like": 0.5,     # burstable instances count at least this share of their vCPUs
    "burst_floor_right": 0.25,
    "k8s_cp_threads": 2, "k8s_cp_ram_gb": 8,  # per control-plane member, 3 per cluster
    "monitoring_overhead": 0.05,  # self-hosted observability stack as a share of app demand
    "backup_factor": 2.0,        # backup TB per TB of primary data (full + incrementals + retention)
    "ops_fte": None,             # override the computed incremental operations effort
    "ops_fte_cost_eur": 110000,  # fully loaded annual cost of one senior platform engineer (Ireland)
    "contingency": 0.10,
    "parallel_run_months": 2,    # months of running both estates during migration
    "providers": None,           # list; default all in catalogue
    "allow_limited": False,      # use limited-stock / promo server configs
}

# Approximate public list prices used ONLY when a line has cost but no usage quantities.
# They convert spend into implied capacity; results are flagged "cost-implied".
REF_USD = {
    "vcpu_month": {"compute": 36.0, "container": 45.0, "serverless": 60.0, "database": 95.0, "cache": 70.0,
                   "search": 80.0, "messaging": 70.0, "kubernetes_control": None, "monitoring": None},
    "ram_per_vcpu": {"compute": 4, "container": 2, "serverless": 2, "database": 8, "cache": 8, "search": 8,
                     "messaging": 4},
    "gb_month": {"block_storage": 0.09, "file_storage": 0.33, "object_storage": 0.024, "backup": 0.05,
                 "database_storage": 0.13, "search_storage": 0.14, "archive_storage": 0.004},
    "gb_transfer": {"egress": 0.085, "cdn": 0.085},
    "gpu_hour": {"T4": 0.75, "A10G": 1.2, "L4": 0.95, "L40S": 2.2, "A100-40": 4.1, "A100-80": 5.1, "H100": 9.0},
}

GPU_RANK = {"T4": 1, "T4G": 1, "P4": 0.5, "P100": 1, "V100": 2, "A2": 1, "A10": 2, "A10G": 2, "L4": 2,
            "RTX-4000-ADA": 1.5, "RTX-PRO-4000": 2, "RTX-6000-ADA": 3, "L40S": 3, "A100-40": 4, "A100-80": 4.5,
            "RTX-PRO-6000": 4.5, "H100": 5, "H200": 6, "B200": 7, "M60": 0.5, "K80": 0.5}


def ceil(x):
    return int(math.ceil(x - 1e-9))


def load_json(p):
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------

def to_eur(amount, currency, A, cat):
    c = (currency or "EUR").upper()
    if c == "EUR":
        return amount
    if c == "USD":
        return amount * A["fx_usd_eur"]
    if c == "GBP":
        return amount * A["fx_gbp_eur"]
    fx = (cat.get("fx") or {}).get(f"{c}_EUR")
    if fx:
        return amount * fx
    raise SystemExit(f"No exchange rate for {c}. Pass --fx-{c.lower()}-eur or add fx.{c}_EUR to the catalogue.")


def item_demand(it, meta, A):
    """Return dict of average concurrent demand for one item, both scenarios."""
    hours = meta["period_hours"] or 730
    months = meta["months_covered"] or 1
    # On usage-level bills, a line with cost but no quantity is a fee (requests, IOPS, API calls),
    # not capacity: only service-level bills get capacity implied from cost.
    may_imply = meta.get("detail_level") != "usage" or it.get("cost_implied_ok")
    spec = decode(it["instance_type"], it.get("cloud")) if it.get("instance_type") else None
    vh = it.get("vcpu_hours", 0.0)
    rh = it.get("ram_gb_hours", 0.0)
    if spec and not vh and it.get("usage_hours"):
        vh = it["usage_hours"] * spec["vcpu"]
        rh = it["usage_hours"] * spec["ram_gb"]
    basis = "usage" if (vh or rh) else None
    cat = it["category"]
    cost_usd_m = it["_cost_usd_month"]
    if not basis and may_imply and cat in REF_USD["vcpu_month"] and REF_USD["vcpu_month"][cat] and cost_usd_m > 0:
        v = cost_usd_m / REF_USD["vcpu_month"][cat]
        vh = v * hours
        rh = v * REF_USD["ram_per_vcpu"].get(cat, 4) * hours
        basis = "cost_implied"
    avg_vcpu = vh / hours
    avg_ram = rh / hours
    smt = True
    gen = None
    burst_like = burst_right = 1.0
    if spec:
        smt = spec["smt"]
        gen = spec["gen_factor"]
        if spec["burstable"]:
            burst_like = min(1.0, max(A["burst_floor_like"], spec["baseline_fraction"] * 2))
            burst_right = max(A["burst_floor_right"], spec["baseline_fraction"])
    else:
        series = (it.get("series") or "").lower()
        if series in ("t2a", "c4a", "t2d"):
            smt = False
        if series in ("n1", "f1", "g1"):
            gen = GEN_FACTOR["legacy"]
        elif series in ("n2", "n2d", "c2", "c2d", "e2"):
            gen = GEN_FACTOR["older"]
        elif series:
            gen = GEN_FACTOR["current"]
        if series in ("e2",):
            burst_like = 1.0
    if gen is None:
        gen = GEN_FACTOR["recent"]
    tf = 1.0 if smt else NO_SMT_THREAD_EQUIV
    like_t = avg_vcpu * tf * burst_like
    burstable = bool(spec and spec["burstable"])
    right_t = avg_vcpu * tf * burst_right * (1.0 if burstable else A["cpu_util"]) * gen
    like_r = avg_ram
    right_r = avg_ram * (A["ram_util"] if cat not in ("database", "cache") else 1.0)
    unit_t = (spec["vcpu"] * tf) if spec else 0
    unit_r = spec["ram_gb"] if spec else 0
    # GPUs
    gpus = it.get("gpu_hours", 0.0) / hours
    if spec and spec["gpu_count"] and it.get("usage_hours"):
        gpus = max(gpus, it["usage_hours"] * spec["gpu_count"] / hours)
    gpu_model = it.get("gpu_model") or (spec or {}).get("gpu_model")
    if cat == "gpu" and not gpus and may_imply and cost_usd_m > 0 and gpu_model in REF_USD["gpu_hour"]:
        gpus = cost_usd_m / (REF_USD["gpu_hour"][gpu_model] * 730)
        basis = basis or "cost_implied"
    # storage and transfer
    gb = it.get("storage_gb_avg") or (it.get("storage_gb_month", 0.0) / max(1, round(months)))
    if not gb and may_imply and cat in REF_USD["gb_month"] and cost_usd_m > 0:
        gb = cost_usd_m / REF_USD["gb_month"][cat]
        basis = basis or "cost_implied"
    elif gb:
        basis = basis or "usage"
    tgb = it.get("transfer_gb", 0.0) / months
    if not tgb and may_imply and cat in REF_USD["gb_transfer"] and cost_usd_m > 0:
        tgb = cost_usd_m / REF_USD["gb_transfer"][cat]
        basis = basis or "cost_implied"
    elif tgb:
        basis = basis or "usage"
    return {
        "basis": basis or "cost_only", "avg_vcpu": avg_vcpu, "avg_ram_gb": avg_ram,
        "like_threads": like_t, "right_threads": right_t, "like_ram": like_r, "right_ram": right_r,
        "unit_threads": unit_t, "unit_ram": unit_r, "gpus": gpus, "gpu_model": gpu_model,
        "unit_gpus": (spec or {}).get("gpu_count", 0) or 0,
        "gpu_mem_gb": (spec or {}).get("gpu_mem_gb"), "storage_gb": gb, "transfer_gb_month": tgb,
        "arch": (spec or {}).get("arch", "x86_64"), "smt": smt, "gen_factor": gen,
    }


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

def pack(servers, T, R, N, unit_t, unit_r, min_nodes, spare, A, label, prefer_nvme=False):
    """Cheapest homogeneous set of servers for demand (threads T, RAM R GB, local NVMe N GB)."""
    cands = []
    for s in servers:
        if s.get("gpu"):
            continue
        if s["threads"] < unit_t or (s["ram_gb"] - A["os_reserve_gb"]) < unit_r:
            continue  # the largest single workload must fit on one server
        ut = s["threads"] * A["cpu_target"]
        ur = (s["ram_gb"] - A["os_reserve_gb"]) * A["ram_target"]
        un = s.get("nvme_raw_gb", 2 * s.get("nvme_gb", 0)) * A["nvme_fill"]
        need = [ceil(T / ut) if T > 0 else 0, ceil(R / ur) if R > 0 else 0]
        if N > 0:
            if un <= 0:
                continue
            need.append(ceil(N / un))
        n = max(need) if need else 0
        if n == 0 and T <= 0 and R <= 0 and N <= 0:
            return None
        n_total = max(n + spare, min_nodes)
        monthly = n_total * s["monthly_eur"]
        setup = n_total * s.get("setup_eur", 0)
        # objective includes per-server overheads (IPv4, marginal operations effort) so the
        # optimiser does not trade a slightly cheaper model for many more machines
        eff = monthly + setup / A["horizon_months"] + n_total * A.get("_per_server_overhead", 0.0)
        cands.append({"_eff": eff, "pool": label, "model": s["model"], "cpu": s.get("cpu"), "count": n_total,
                    "threads_each": s["threads"], "ram_gb_each": s["ram_gb"], "nvme_gb_each": s.get("nvme_gb", 0),
                    "monthly_eur_each": s["monthly_eur"], "setup_eur_each": s.get("setup_eur", 0),
                    "monthly_eur": round(monthly, 2), "setup_eur": round(setup, 2),
                    "binding_constraint": ["cpu", "ram", "nvme"][need.index(max(need))] if n >= min_nodes - spare and need and max(need) > 0 else "minimum nodes",
                    "utilisation": {
                        "cpu": round(T / (n_total * s["threads"]), 3) if n_total else 0,
                        "ram": round(R / (n_total * (s["ram_gb"] - A["os_reserve_gb"])), 3) if n_total else 0,
                        "nvme_raw": round(N / (n_total * s.get("nvme_raw_gb", 2 * s.get("nvme_gb", 0))), 3) if N and s.get("nvme_gb") else 0},
                    "source": s.get("source"), "verified": s.get("verified", True)})
    if not cands:
        return None
    # among options within 3% of the cheapest, prefer fewer machines (less to run, fewer failure points)
    floor = min(c["_eff"] for c in cands)
    near = [c for c in cands if c["_eff"] <= floor * 1.03]
    best = min(near, key=lambda c: (c["count"], c["_eff"]))
    best = dict(best)
    best.pop("_eff")
    return best


def pack_gpu(servers, gpu_demand, A):
    """gpu_demand: {model: {"gpus": avg, "mem": gb}} -> list of server picks, list of unmatched."""
    picks, unmatched = [], []
    for model, d in gpu_demand.items():
        if model not in GPU_RANK or model == "unknown":
            unmatched.append(model)  # unknown GPU or cloud-only accelerator (Inferentia, Trainium, TPU)
            continue
        need_rank = GPU_RANK[model]
        need_mem = d.get("mem") or 16
        # 4-8 data-centre GPUs in one instance usually means NVLink-coupled training or large-model
        # serving: they must stay in one box, not be split across single-GPU servers
        need_per_box = d.get("unit_gpus", 0) if (d.get("unit_gpus", 0) >= 4 and need_rank >= 4) else 0
        cands = [s for s in servers if s.get("gpu") and s["gpu"].get("mem_gb", 0) >= need_mem
                 and GPU_RANK.get(s["gpu"].get("model"), 0) >= need_rank
                 and s["gpu"].get("count", 1) >= need_per_box]
        if not cands:
            unmatched.append(model)
            continue
        best = None
        for s in cands:
            per = s["gpu"].get("count", 1)
            n = max(1, ceil(d["gpus"] / per))
            eff = n * (s["monthly_eur"] + s.get("setup_eur", 0) / A["horizon_months"])
            if best is None or eff < best[0]:
                best = (eff, s, n)
        _, s, n = best
        picks.append({"pool": f"gpu ({model})", "model": s["model"], "cpu": s.get("cpu"), "count": n,
                      "gpu": s["gpu"], "threads_each": s["threads"], "ram_gb_each": s["ram_gb"],
                      "nvme_gb_each": s.get("nvme_gb", 0), "monthly_eur_each": s["monthly_eur"],
                      "setup_eur_each": s.get("setup_eur", 0), "monthly_eur": round(n * s["monthly_eur"], 2),
                      "setup_eur": round(n * s.get("setup_eur", 0), 2), "replaces_avg_gpus": round(d["gpus"], 2),
                      "source": s.get("source"), "verified": s.get("verified", True)})
    return picks, unmatched


# ---------------------------------------------------------------------------
# Main estimate
# ---------------------------------------------------------------------------

def estimate(estate, catalogue, A):
    meta = estate["meta"]
    months = meta.get("months_covered") or 1
    currency = meta.get("currency") or "USD"
    flags = []
    if A["fx_usd_eur"] is None:
        A["fx_usd_eur"] = catalogue["fx"]["USD_EUR"]
    if A["fx_gbp_eur"] is None:
        A["fx_gbp_eur"] = catalogue["fx"].get("GBP_EUR", 1.15)
    usd_per_cur = 1.0 if currency.upper() == "USD" else to_eur(1.0, currency, A, catalogue) / A["fx_usd_eur"]

    disp_over = A.get("dispositions") or {}
    items = []
    baseline = 0.0
    by_disp = {"migrate": 0.0, "retain": 0.0, "eliminate": 0.0, "baseline_only": 0.0, "exclude": 0.0}
    by_cat = {}
    for it in estate["items"]:
        it = dict(it)
        cat = it["category"]
        d = disp_over.get(it.get("service") or "", disp_over.get(cat, DEFAULT_DISPOSITION.get(cat, "retain")))
        if cat == "messaging" and not it.get("usage_hours") and d == "migrate":
            d = "retain"  # queue/stream APIs (SQS, Pub/Sub, Service Bus) are cheap to keep or swap for NATS later
        if it.get("disposition"):
            d = it["disposition"]
        it["_disp"] = d
        it["_cost_eur_month"] = to_eur(it["cost"], currency, A, catalogue) / months
        it["_cost_usd_month"] = it["cost"] * usd_per_cur / months
        it["_dem"] = item_demand(it, meta, A)
        if d != "exclude":
            baseline += it["_cost_eur_month"]
        by_disp[d] = by_disp.get(d, 0.0) + it["_cost_eur_month"]
        by_cat.setdefault(cat, {"cost_eur_month": 0.0, "disposition": d})
        by_cat[cat]["cost_eur_month"] += it["_cost_eur_month"]
        items.append(it)

    # ---- flags from content
    win = [i for i in items if i.get("os") == "windows" and i["_disp"] == "migrate"]
    if win:
        v = sum(i["_dem"]["avg_vcpu"] for i in win)
        flags.append({"level": "warn", "text": f"Windows workloads (~{v:.0f} average vCPU) were licence-included in the cloud. "
                      "Windows Server licences are not priced here; budget for them or plan a move to Linux."})
    lic_db = sorted({i.get("engine") for i in items if i.get("engine") in ("sqlserver", "oracle", "db2") and i["_disp"] == "migrate"})
    if lic_db:
        flags.append({"level": "warn", "text": f"Commercial database engines ({', '.join(lic_db)}) need their own licences "
                      "on bare metal (not priced here). Consider PostgreSQL as part of the move."})
    spot = sum(i["_cost_eur_month"] for i in items if i.get("pricing_model") == "spot")
    if spot > 0.03 * baseline:
        flags.append({"level": "info", "text": "A noticeable share of compute runs on spot/preemptible capacity, which is "
                      "already discounted; savings on that part will be smaller than on on-demand capacity."})
    aur = [i for i in items if i.get("replicas_included")]
    if aur:
        flags.append({"level": "info", "text": "Some managed data services bill replicas separately (e.g. Aurora, "
                      "Azure HA standbys); replica counts were taken as billed."})
    arch = sum(i["_dem"]["avg_vcpu"] for i in items if i["_dem"]["arch"] == "arm64" and i["_disp"] == "migrate")
    if arch > 0:
        flags.append({"level": "info", "text": f"~{arch:.0f} average vCPU run on Arm (Graviton/Ampere/Axion/Cobalt). "
                      "The plan sizes them onto x86 thread equivalents; Arm bare metal is an option where offered."})

    # ---- aggregate demand
    pools = {"app": {"like_t": 0, "right_t": 0, "like_r": 0, "right_r": 0, "unit_t": 0, "unit_r": 0},
             "data": {"like_t": 0, "right_t": 0, "like_r": 0, "right_r": 0, "unit_t": 0, "unit_r": 0,
                      "nvme_gb": 0}}
    gpu_demand = {}
    engines = set()
    clusters = 0.0
    storage = {"block_gb": 0.0, "file_gb": 0.0, "object_gb": 0.0, "backup_cloud_gb": 0.0, "data_gb": 0.0}
    egress_gb = 0.0
    lbs_cost = 0.0
    basis_cost = {"usage": 0.0, "cost_implied": 0.0, "cost_only": 0.0}
    retained = 0.0
    retained_lines = {}
    hours = meta["period_hours"] or 730
    for it in items:
        d, dem, cat = it["_disp"], it["_dem"], it["category"]
        if d == "retain":
            retained += it["_cost_eur_month"]
            retained_lines[cat] = retained_lines.get(cat, 0.0) + it["_cost_eur_month"]
            continue
        if d != "migrate":
            continue
        basis_cost[dem["basis"] if dem["basis"] in basis_cost else "cost_only"] += max(0.0, it["_cost_eur_month"])
        pool = POOL_OF.get(cat)
        if cat == "kubernetes_control":
            clusters += (it.get("usage_hours") or 0) / hours if it.get("usage_hours") else (1 if it["_cost_eur_month"] > 0 else 0)
            continue
        if cat == "monitoring":
            continue  # handled as overhead on the app pool
        if cat == "gpu":
            g = gpu_demand.setdefault(dem["gpu_model"] or "unknown",
                                      {"gpus": 0.0, "mem": dem["gpu_mem_gb"] or GPU_MEM.get(dem["gpu_model"] or "", 16),
                                       "unit_gpus": 0})
            g["gpus"] += dem["gpus"]
            g["unit_gpus"] = max(g["unit_gpus"], dem["unit_gpus"])
            # host CPU/RAM of GPU instances is provided by the GPU server itself
            continue
        if it.get("standby"):
            continue  # a billed HA standby: the db_replicas factor on the primary already provides it
        if pool in ("app", "data"):
            p = pools[pool]
            rep = 1
            if cat == "database" and not it.get("replicas_included"):
                rep = A["db_replicas"]
            p["like_t"] += dem["like_threads"] * rep
            p["right_t"] += dem["right_threads"] * rep
            p["like_r"] += dem["like_ram"] * rep
            p["right_r"] += dem["right_ram"] * rep
            p["unit_t"] = max(p["unit_t"], dem["unit_threads"])
            p["unit_r"] = max(p["unit_r"], dem["unit_ram"])
            if pool == "data" and it.get("engine"):
                engines.add(it["engine"])
            elif pool == "data":
                engines.add(cat)
        if cat in ("database_storage", "search_storage", "cache_storage"):
            rep = A["db_replicas"] if cat == "database_storage" else 1
            # local NVMe, one copy per replica (the replica is the redundancy, as with cloud disks), plus growth
            pools["data"]["nvme_gb"] += dem["storage_gb"] * rep * (1 + A["growth_headroom"])
            storage["data_gb"] += dem["storage_gb"]
        elif cat == "block_storage":
            storage["block_gb"] += dem["storage_gb"]
        elif cat == "file_storage":
            storage["file_gb"] += dem["storage_gb"]
        elif cat == "object_storage":
            storage["object_gb"] += dem["storage_gb"]
        elif cat == "backup":
            storage["backup_cloud_gb"] += dem["storage_gb"]
        elif cat == "egress":
            egress_gb += dem["transfer_gb_month"]
        elif cat == "load_balancer":
            lbs_cost += it["_cost_eur_month"]

    # monitoring overhead and k8s control planes on the app pool
    ap = pools["app"]
    ap["like_t"] *= (1 + A["monitoring_overhead"])
    ap["right_t"] *= (1 + A["monitoring_overhead"])
    ap["like_r"] *= (1 + A["monitoring_overhead"])
    ap["right_r"] *= (1 + A["monitoring_overhead"])
    n_clusters = max(0, round(clusters)) if clusters else 0
    if n_clusters:
        for k in ("like_t", "right_t"):
            ap[k] += n_clusters * 3 * A["k8s_cp_threads"]
        for k in ("like_r", "right_r"):
            ap[k] += n_clusters * 3 * A["k8s_cp_ram_gb"]

    shared_gb = (storage["block_gb"] + storage["file_gb"]) * (1 + A["growth_headroom"])
    shared_raw = shared_gb * A["block_replication"]
    shared_raw_right = shared_raw * A["block_used_fraction"]
    data_nvme_right = pools["data"]["nvme_gb"] * A["block_used_fraction"]
    primary_data_gb = storage["block_gb"] + storage["file_gb"] + storage["data_gb"]
    backup_tb = primary_data_gb * A["backup_factor"] / 1024

    demand_summary = {
        "app_pool": {k: round(v, 2) for k, v in ap.items()},
        "data_pool": {k: round(v, 2) for k, v in pools["data"].items()},
        "data_engines": sorted(engines),
        "kubernetes_clusters": n_clusters,
        "gpu": {k: {"avg_gpus": round(v["gpus"], 2), "mem_gb": v["mem"], "per_instance": v.get("unit_gpus", 0)}
                for k, v in gpu_demand.items()},
        "storage_gb": {k: round(v, 1) for k, v in storage.items()},
        "shared_block_raw_gb": round(shared_raw, 1),
        "shared_block_raw_gb_right_sized": round(shared_raw_right, 1),
        "egress_gb_month": round(egress_gb, 1),
        "egress_avg_mbps": round(egress_gb * 8 * 1024 / (730 * 3600), 1),
        "backup_target_tb": round(backup_tb, 2),
    }

    providers = A["providers"] or [k for k, v in catalogue["providers"].items() if v.get("default_enabled", True)]
    results = {}
    for pk in providers:
        P = catalogue["providers"].get(pk)
        if not P:
            flags.append({"level": "warn", "text": f"Provider '{pk}' is not in the catalogue; skipped."})
            continue
        servers = [s for s in P["servers"] if s.get("monthly_eur")
                   and (A["allow_limited"] or s.get("availability", "standard") != "limited")]
        general = [s for s in servers if not s.get("gpu") and "storage" not in s.get("role", [])]
        storage_srv = [s for s in servers if not s.get("gpu")]
        res = {"name": P["name"], "locations": P.get("locations"), "scenarios": {},
               "unverified_prices": any(not x.get("verified", True) for x in P["servers"])}
        res["pricing_caveat"] = P.get("pricing_caveat")
        A["_per_server_overhead"] = P.get("per_server_monthly_eur", 0.0) + \
            (0.02 * A["ops_fte_cost_eur"] / 12 if A["ops_fte"] is None else 0.0)
        for scen in ("like_for_like", "right_sized"):
            t_key, r_key = ("like_t", "like_r") if scen == "like_for_like" else ("right_t", "right_r")
            sh_raw = shared_raw if scen == "like_for_like" else shared_raw_right
            dp_nvme = pools["data"]["nvme_gb"] if scen == "like_for_like" else data_nvme_right
            picks = []
            # option A: shared block storage colocated on app nodes; option B: separate storage nodes
            options = []
            app_a = pack(general, ap[t_key], ap[r_key], sh_raw, ap["unit_t"], ap["unit_r"],
                         A["app_min_nodes"], A["app_spare_nodes"], A, "app") if (ap[t_key] or sh_raw) else None
            if app_a or not (ap[t_key] or sh_raw):
                options.append(("colocated", [p for p in [app_a] if p]))
            if sh_raw > 0:
                app_b = pack(general, ap[t_key], ap[r_key], 0, ap["unit_t"], ap["unit_r"],
                             A["app_min_nodes"], A["app_spare_nodes"], A, "app") if ap[t_key] else None
                st_b = pack(storage_srv, 0, 0, sh_raw, 0, 0, A["storage_min_nodes"], 0, A, "storage (Ceph)")
                if st_b and (app_b or not ap[t_key]):
                    options.append(("separate storage", [p for p in [app_b, st_b] if p]))
            dp = pools["data"]
            data_pick = None
            if dp[t_key] or dp_nvme:
                data_pick = pack(general, dp[t_key], dp[r_key], dp_nvme, dp["unit_t"], dp["unit_r"],
                                 A["data_min_nodes"], 0, A, "data")
            # option C (small estates): one consolidated cluster for app + data
            if dp[t_key] or dp_nvme:
                cons = pack(general, ap[t_key] + dp[t_key], ap[r_key] + dp[r_key], sh_raw + dp_nvme,
                            max(ap["unit_t"], dp["unit_t"]), max(ap["unit_r"], dp["unit_r"]),
                            A["app_min_nodes"], A["app_spare_nodes"], A, "consolidated (app + data)")
            else:
                cons = None
            def cost_of(ps):
                return sum(p["monthly_eur"] + p["setup_eur"] / A["horizon_months"] + p["count"] * A["_per_server_overhead"]
                           for p in ps)
            best_layout, best_picks = None, None
            for name, ps in options:
                full = ps + ([data_pick] if data_pick else [])
                if (dp[t_key] or dp_nvme) and not data_pick:
                    continue
                if best_picks is None or cost_of(full) < cost_of(best_picks):
                    best_layout, best_picks = name + (" + data pool" if data_pick else ""), full
            if cons and (best_picks is None or cost_of([cons]) < cost_of(best_picks)):
                best_layout, best_picks = "consolidated", [cons]
            if best_picks is None:
                best_picks, best_layout = [], "none"
                if ap[t_key] or dp[t_key]:
                    flags.append({"level": "warn", "text": f"{P['name']}: no catalogue server is large enough for the "
                                  f"largest single workload ({max(ap['unit_t'], dp['unit_t']):.0f} threads / "
                                  f"{max(ap['unit_r'], dp['unit_r']):.0f} GB). Add larger configs to the catalogue."})
            picks.extend(best_picks)
            gpicks, unmatched = pack_gpu(servers, gpu_demand, A)
            picks.extend(gpicks)
            gpu_retained = 0.0
            if unmatched:
                for it in items:
                    if it["category"] == "gpu" and (it["_dem"]["gpu_model"] or "unknown") in unmatched and it["_disp"] == "migrate":
                        gpu_retained += it["_cost_eur_month"]

            n_servers = sum(p["count"] for p in picks)
            servers_m = sum(p["monthly_eur"] for p in picks)
            setup = sum(p["setup_eur"] for p in picks)
            # object storage
            obj = P.get("object_storage") or {}
            obj_tb = storage["object_gb"] / 1024 * (1 + A["growth_headroom"])
            obj_m = 0.0
            if obj_tb > 0 and obj:
                obj_m = obj.get("base_monthly_eur", 0) + max(0.0, obj_tb - obj.get("included_tb", 0)) * obj["eur_per_tb_month"]
            # backup
            bk = P.get("backup_storage") or {}
            bk_m = backup_tb * bk.get("eur_per_tb_month", 0) if backup_tb > 0 else 0.0
            if backup_tb > 0 and bk.get("min_monthly_eur"):
                bk_m = max(bk_m, bk["min_monthly_eur"])
            # traffic
            tr = P.get("traffic") or {}
            inc = tr.get("included_tb_per_server")
            egress_tb = egress_gb / 1024
            tr_m = 0.0
            if inc is not None and egress_tb > 0:
                tr_m = max(0.0, egress_tb - inc * max(1, n_servers)) * tr.get("overage_eur_per_tb", 1.0)
            port_mbps = tr.get("port_gbps", 1) * 1000
            peak_mbps = demand_summary["egress_avg_mbps"] * 3  # assume peak ~3x average
            uplink_m = 0.0
            if peak_mbps > 0.6 * port_mbps * max(1, n_servers) and tr.get("uplink_10g_monthly_eur"):
                n_up = min(n_servers, ceil(peak_mbps / (0.6 * 10000)))
                uplink_m = n_up * tr["uplink_10g_monthly_eur"]
            # load balancers
            lb = P.get("load_balancer") or {}
            lb_m = (2 * lb.get("monthly_eur", 0)) if lbs_cost > 0 else 0.0
            extras_m = P.get("fixed_monthly_eur", 0.0) + n_servers * P.get("per_server_monthly_eur", 0.0)
            infra = servers_m + obj_m + bk_m + tr_m + uplink_m + lb_m + extras_m
            cont = infra * A["contingency"]
            # operations effort
            if A["ops_fte"] is not None:
                fte = A["ops_fte"]
                fte_basis = "given"
            else:
                # incremental effort vs. running the same estate on a hyperscaler: hardware lifecycle,
                # OS/firmware patching, self-run data services, storage. Scales sub-linearly with fleet size.
                fte = 0.10 + 0.02 * n_servers + 0.05 * len(engines) + (0.05 if sh_raw > 0 else 0) + \
                    (0.05 if n_clusters else 0)
                fte = round(max(0.25, min(fte, 0.5 + 0.02 * n_servers)), 2)
                fte_basis = "computed"
            ops_m = fte * A["ops_fte_cost_eur"] / 12
            one_fte_m = A["ops_fte_cost_eur"] / 12
            retained_total = retained + gpu_retained
            total = infra + cont + retained_total + ops_m
            total_ex_ops = infra + cont + retained_total
            saving = baseline - total
            saving_ex_ops = baseline - total_ex_ops
            parallel = A["parallel_run_months"] * (infra)
            one_off = setup + parallel
            res["scenarios"][scen] = {
                "layout": best_layout, "servers": picks, "server_count": n_servers,
                "monthly_eur": {
                    "servers": round(servers_m, 2), "object_storage": round(obj_m, 2), "backup_storage": round(bk_m, 2),
                    "traffic_overage": round(tr_m, 2), "uplinks": round(uplink_m, 2), "load_balancers": round(lb_m, 2),
                    "fixed_extras": round(extras_m, 2), "contingency": round(cont, 2),
                    "retained_cloud_services": round(retained_total, 2), "operations_effort": round(ops_m, 2),
                    "total": round(total, 2), "total_excluding_operations": round(total_ex_ops, 2)},
                "ops_fte": fte, "ops_fte_basis": fte_basis,
                "gpu_unmatched": unmatched,
                "saving_eur_month": round(saving, 2), "saving_pct": round(100 * saving / baseline, 1) if baseline else None,
                "saving_excluding_operations_eur_month": round(saving_ex_ops, 2),
                "saving_excluding_operations_pct": round(100 * saving_ex_ops / baseline, 1) if baseline else None,
                "saving_with_one_fte_eur_month": round(baseline - (total_ex_ops + one_fte_m), 2),
                "one_off_eur": {"setup_fees": round(setup, 2), "parallel_running": round(parallel, 2),
                                "total": round(one_off, 2)},
                "payback_months": round(one_off / saving, 1) if saving > 0 else None,
            }
        results[pk] = res

    # recommended provider: cheapest right-sized total
    rec = None
    for pk, r in results.items():
        t = r["scenarios"]["right_sized"]["monthly_eur"]["total"]
        if rec is None or t < results[rec]["scenarios"]["right_sized"]["monthly_eur"]["total"]:
            rec = pk

    migrate_cost = by_disp.get("migrate", 0.0)
    usage_share = basis_cost["usage"] / migrate_cost if migrate_cost > 0 else 0
    mapped_share = migrate_cost / baseline if baseline > 0 else 0
    if usage_share >= 0.8 and mapped_share >= 0.6 and meta.get("detail_level") == "usage":
        confidence = "medium-high"
    elif usage_share >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"
    if months < 0.9:
        confidence = "low"
    commit = [i for i in items if i.get("pricing_model") in ("reserved", "savings_plan", "committed")
              or i["category"] in ("commitment", "discount")]
    if commit:
        flags.append({"level": "warn", "text": "Your bill shows reserved, savings-plan, committed-use or negotiated "
                      "discount pricing. Remaining commitment terms and any minimum-spend agreement (EDP, MACC, "
                      "committed use) are not in this estimate; check them before timing a move."})
    lic = by_cat.get("licence", {}).get("cost_eur_month", 0)
    if lic > 0:
        flags.append({"level": "warn", "text": f"Software licences billed through the cloud (about €{lic:,.0f}/mo) are "
                      "kept at today's cost; check whether they can move with you or must be bought again."})
    for pk_, pr in results.items():
        um = sorted({m for sc in pr["scenarios"].values() for m in sc["gpu_unmatched"]})
        if um:
            flags.append({"level": "warn", "provider": pk_,
                          "text": f"{pr['name']} has no catalogued server matching {', '.join(um)}; in its figures those "
                                  "GPUs or accelerators stay in the cloud at today's cost."})
    seen = {k: v for k, v in (meta.get("currencies_seen") or {}).items() if v > 0}
    if len(seen) > 1:
        flags.append({"level": "warn", "text": f"The bill mixes currencies ({', '.join(seen)}); every line was treated as "
                      f"{currency}. Split the files by currency and run them separately for an accurate baseline."})
    if months < 0.9:
        flags.append({"level": "warn", "text": f"The bill covers only {months:.2f} months; averages may not represent a full month."})
    if meta.get("credits"):
        flags.append({"level": "info", "text": f"Credits of {abs(meta['credits']):,.0f} {currency} were excluded from the baseline; "
                      "they are temporary and would not continue indefinitely."})
    unclassified = sum(x["cost"] for x in meta.get("unclassified_top", []))
    if baseline and to_eur(unclassified, currency, A, catalogue) / months > 0.05 * baseline:
        flags.append({"level": "warn", "text": "More than 5% of spend could not be classified and was retained as-is. "
                      "Review the 'other' lines and set dispositions to tighten the estimate."})

    archive_m = by_cat.get("archive_storage", {}).get("cost_eur_month", 0)
    if archive_m > 0:
        flags.append({"level": "info", "text": "Archive-tier storage (Glacier/Archive/Coldline) is kept where it is: "
                      "it is already cheaper per TB than most bare-metal options."})

    # one-off data transfer out at list price (for the Data Act / exit-waiver note)
    total_data_gb = storage["object_gb"] + primary_data_gb
    exit_list_eur = total_data_gb * REF_USD["gb_transfer"]["egress"] * A["fx_usd_eur"]

    return {
        "schema": "counterweight.plan/1",
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "catalogue_as_of": catalogue.get("as_of"),
        "input": {"period_start": meta.get("period_start"), "period_end": meta.get("period_end"),
                  "months_covered": months, "currency": currency, "input_kind": meta.get("input_kind"),
                  "cost_basis": meta.get("cost_basis"), "detail_level": meta.get("detail_level"),
                  "files": meta.get("input_files")},
        "baseline_eur_month": round(baseline, 2),
        "baseline_by_disposition_eur_month": {k: round(v, 2) for k, v in by_disp.items() if v},
        "baseline_by_category_eur_month": {k: {"cost": round(v["cost_eur_month"], 2), "disposition": v["disposition"]}
                                            for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]["cost_eur_month"])},
        "retained_by_category_eur_month": {k: round(v, 2) for k, v in sorted(retained_lines.items(), key=lambda kv: -kv[1])},
        "demand": demand_summary,
        "confidence": {"grade": confidence, "share_of_spend_sized": round(mapped_share, 3),
                       "share_of_sized_spend_from_usage_data": round(usage_share, 3),
                       "share_of_sized_spend_cost_implied": round(basis_cost["cost_implied"] / migrate_cost, 3) if migrate_cost else 0},
        "providers": results,
        "recommended_provider": rec,
        "exit_transfer_list_price_eur": round(exit_list_eur, 0),
        "invoice_total_eur_period": round(baseline * months, 2),
        "exit_data_tb": round(total_data_gb / 1024, 2),
        "assumptions": {k: v for k, v in A.items() if k != "dispositions" and not k.startswith("_")},
        "dispositions_overridden": disp_over,
        "flags": flags,
        "top_items": [{"category": i["category"], "service": i.get("service"), "instance_type": i.get("instance_type"),
                       "engine": i.get("engine"), "disposition": i["_disp"], "cost_eur_month": round(i["_cost_eur_month"], 2),
                       "avg_vcpu": round(i["_dem"]["avg_vcpu"], 2), "avg_ram_gb": round(i["_dem"]["avg_ram_gb"], 2),
                       "basis": i["_dem"]["basis"], "resources": i.get("resources", 0)}
                      for i in sorted(items, key=lambda x: -x["_cost_eur_month"])[:40]],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("estate")
    ap.add_argument("-o", "--output", default="plan.json")
    ap.add_argument("--catalogue", default=DEFAULT_CATALOGUE)
    ap.add_argument("--assumptions", help="JSON file overriding any default in DEFAULTS, plus 'dispositions'")
    ap.add_argument("--fx-usd-eur", type=float)
    ap.add_argument("--fx-gbp-eur", type=float)
    ap.add_argument("--providers", help="comma-separated catalogue keys")
    ap.add_argument("--cpu-util", type=float, help="right-sized CPU factor (e.g. measured p95 / 100 * 1.25)")
    ap.add_argument("--ram-util", type=float)
    ap.add_argument("--ops-fte", type=float, help="incremental operations FTE (overrides the computed value)")
    ap.add_argument("--ops-fte-cost", type=float, help="fully loaded annual EUR per FTE")
    ap.add_argument("--contingency", type=float)
    ap.add_argument("--allow-partial", action="store_true", help="estimate even when the bill covers under a week")
    a = ap.parse_args(argv)

    A = dict(DEFAULTS)
    if a.assumptions:
        A.update(load_json(a.assumptions))
    for k, v in (("fx_usd_eur", a.fx_usd_eur), ("fx_gbp_eur", a.fx_gbp_eur), ("cpu_util", a.cpu_util),
                 ("ram_util", a.ram_util), ("ops_fte", a.ops_fte), ("ops_fte_cost_eur", a.ops_fte_cost),
                 ("contingency", a.contingency)):
        if v is not None:
            A[k] = v
    if a.providers:
        A["providers"] = [p.strip() for p in a.providers.split(",") if p.strip()]

    estate = load_json(a.estate)
    catalogue = load_json(a.catalogue)
    mc = estate["meta"].get("months_covered") or 0
    if mc < 0.25 and not a.allow_partial:
        print(f"error: the bill covers {mc * 730:.0f} hours. Use at least one full month (or pass --allow-partial "
              "to estimate anyway, with low confidence).", file=sys.stderr)
        return 4
    plan = estimate(estate, catalogue, A)
    with open(a.output, "w") as f:
        json.dump(plan, f, indent=1)

    rec = plan["recommended_provider"]
    out = {"output": a.output, "baseline_eur_month": plan["baseline_eur_month"],
           "confidence": plan["confidence"]["grade"], "recommended": rec, "providers": {}}
    for pk, r in plan["providers"].items():
        out["providers"][pk] = {s: {"total": v["monthly_eur"]["total"], "ex_ops": v["monthly_eur"]["total_excluding_operations"],
                                    "saving_pct": v["saving_pct"], "servers": v["server_count"], "layout": v["layout"]}
                                for s, v in r["scenarios"].items()}
    out["flags"] = [f["text"][:140] for f in plan["flags"]]
    print(json.dumps(out, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
