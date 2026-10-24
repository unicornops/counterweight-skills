# Sizing method

How `estimate.py` turns a bill into servers and euros, every default it uses, and why. Override any default with `--assumptions file.json` (same key names) or the CLI flags.

## Contents

1. Demand from the bill
2. Scenarios
3. Pools, headroom and redundancy
4. Choosing servers
5. Everything else that costs money
6. The baseline and the saving
7. Confidence
8. Sanity checklist

## 1. Demand from the bill

Every item becomes **average concurrent demand** over the billing period:

- `avg_vcpu = vcpu_hours / period_hours`, `avg_ram = ram_gb_hours / period_hours`. Instance hours are converted with the decoded instance shape (`specs.py`).
- Averages are right for steady fleets (the norm for VMs, Kubernetes nodes and databases). Autoscaled estates with large daily swings are under-counted at peak; the app-pool spare server and 70% CPU ceiling absorb moderate swings. Flag strongly spiky estates and suggest the Teardown measures peaks.
- **Thread equivalents.** One cloud vCPU on x86 is one SMT thread. One vCPU on Arm (Graviton, Ampere, Axion, Cobalt) or on x86 with SMT off (GCP T2D) is a whole core, counted as **1.4** threads (`NO_SMT_THREAD_EQUIV`). A busy SMT sibling delivers roughly 60–70% of a core, so a whole core ≈ 1.4–1.6 threads; 1.4 is the conservative end.
- **Burstable instances** (t2/t3/t4g, Azure B, GCP e2 shared-core) count at their baseline share: like-for-like `max(0.5, 2 × baseline)`, right-sized `max(0.25, baseline)` of their vCPUs. Right-sized does not apply `cpu_util` on top, since the baseline already reflects use.
- **SMT off:** AWS AMD families from the 7th generation (m7a, c7a, r7a, and their 8th-generation successors) expose one vCPU per physical core, like Graviton, and count as 1.4 threads.
- **OCI OCPUs:** OCI bills OCPU-hours and memory GB-hours per shape series. On x86 one OCPU is two vCPUs (a core with both threads); on Ampere A1 it is one Arm core, on A2 and A4 two Arm cores, and Arm cores count as 1.4 threads each. The series sets the generation factor (E2 and X7 legacy, E3 older, E4, X9 and A1 recent, E5, E6, X12, A2 and A4 current).
- **IBM Cloud** VPC profiles carry their shape (`bx2-4x16`: 4 vCPU, 16 GiB; generation 2 counts as older, 3 and 4 as current). IBM Cloud Databases bill every member, so their quantities already include replicas.
- **Cost-only lines on service-level bills** (PDF invoices, service totals) get cost-implied capacity from approximate list prices (`REF_USD` in `estimate.py`): e.g. $36 per general-purpose vCPU-month with 4 GB/vCPU, $95 per managed-database vCPU-month with 8 GB/vCPU. These lines are marked `cost_implied` and lower the confidence grade. On usage-level bills a line with cost but no quantity is a fee (requests, IOPS, API calls), not capacity, so nothing is implied from it (set `cost_implied_ok` on an item to override). Cost-implied database capacity is deliberately generous (the line often includes storage and backups), which errs towards more bare metal, not less.
- **Serverless:** Lambda GB-seconds → GB-hours of memory, with one vCPU per 1.769 GB (Lambda's allocation ratio). Fargate and Cloud Run bill vCPU and memory directly.
- **Storage** is the average GB held: GB-months ÷ whole calendar months (providers measure GB-months against the actual month length). **Transfer** is GB per month.

## 2. Scenarios

| | Like-for-like | Right-sized |
|---|---|---|
| CPU | 1 bare-metal thread per cloud vCPU (thread-equivalent) | × `cpu_util` (0.6) × generation factor |
| RAM | GB for GB | × `ram_util` (0.9); databases and caches keep 100% |
| Disk | Provisioned GB | × `block_used_fraction` (0.7) |

**Generation factor** (`GEN_FACTOR` in `specs.py`): per-thread speed of the cloud CPU relative to a current bare-metal thread (Zen 4/5 EPYC or Ryzen, Granite Rapids). Legacy (AWS gen ≤4, Azure v2/v3, GCP N1) 0.60; older (AWS gen 5, Azure v4, GCP N2/C2/E2) 0.70; recent (AWS gen 6, Azure v5, Graviton2) 0.85; current 1.00. These are planning factors from public single-thread benchmarks, not measurements of this workload.

**`cpu_util` 0.6** reflects that most cloud fleets are sized for peak with average CPU well under 40%; sizing to p95 with 25% headroom typically lands around 0.5–0.7 of the provisioned vCPU. When the person gives real numbers, use `p95 / 100 × 1.25` (floor 0.3).

Present both. Like-for-like is what a cautious engineer would buy without measuring anything; right-sized is what a Teardown usually finds. The truth is normally between them.

## 3. Pools, headroom and redundancy

| Pool | Holds | Redundancy | Minimum |
|---|---|---|---|
| app | VMs, containers, serverless, Kubernetes workers; +5% for the self-hosted monitoring stack; +3 control-plane members (2 threads, 8 GB each) per managed Kubernetes cluster | one spare server (N+1) | 3 servers (scheduler quorum) |
| data | Managed databases, caches, search, brokers; their storage on local NVMe | databases get a replica on a second server (`db_replicas` 2) unless the bill already lists replicas; caches/search/brokers are taken as billed (their nodes are billed individually) | 2 servers |
| storage | Disks and file shares as software-replicated volumes (Longhorn/Ceph), 2 copies | replication | 3 servers if separate |
| gpu | GPU instances, matched by GPU memory and class | none added | 1 per GPU needed |

- Planned ceilings: **70% CPU** (`cpu_target`), **85% RAM** after a 4 GB OS reserve (`ram_target`, `os_reserve_gb`), **75% NVMe fill** (`nvme_fill`).
- Data growth headroom **25%** on all stored data.
- Shared disks use `nvme_raw_gb` (no RAID, software replication). Database storage also uses raw NVMe, one copy per replica: the replica is the redundancy, as with a cloud disk.
- **Largest single workload must fit on one server**: every pool records its largest instance (threads and RAM) and rejects server models smaller than it.

## 4. Choosing servers

For each provider and scenario, `estimate.py` tries three layouts and keeps the cheapest:

1. **Colocated:** app pool and shared storage on the same servers, plus a separate data pool.
2. **Separate storage:** app pool, data pool and dedicated storage servers.
3. **Consolidated:** one pool for everything (wins for small estates, where the minimum node counts dominate).

Within a pool every server is the same model (homogeneous pools are simpler to run). The objective per model is `monthly + setup / 36 + per-server overhead`, where the overhead is the provider's per-server extras (IPv4 at Hetzner) plus 0.02 of an engineer's monthly cost, so the optimiser doesn't trade a marginally cheaper model for many more machines. Among models within 3% of the cheapest, it picks the one with the fewest servers.

GPU demand is matched by GPU memory (≥ the cloud GPU's) and class rank (`GPU_RANK`): T4 < A10G ≈ L4 ≈ RTX PRO 4000 < L40S < A100 ≈ RTX PRO 6000 < H100 < H200 < B200. Instances with 4 or more data-centre-class GPUs (A100 and up) are treated as NVLink-coupled: they only match a server with at least as many GPUs in one box. Unknown GPUs, cloud-only accelerators (Inferentia, Trainium) and unmatched types stay in the cloud at today's cost and are flagged per provider. GPU servers get no spare.

Limited-stock configurations (`availability: limited`) are skipped unless `allow_limited` is true: you can't build a fleet on them.

**Which providers.** Hetzner, OVHcloud and Scaleway are priced by default: each publishes official euro prices excluding VAT, has EU locations, sells S3-compatible object storage and a load balancer, and has stock that can carry a fleet. Leaseweb, Cherry Servers, Latitude.sh, phoenixNAP, Teraswitch and Vultr are opt-in (`--providers default,<key>` or `all`) because each has a gap: unverified shop data (Leaseweb), thin stock and no object storage (Cherry Servers), US-dollar prices with an unclear VAT basis (Latitude.sh, Teraswitch), one European site (phoenixNAP), or no deployable large plans (Vultr). `references/refreshing-prices.md` has the full reasoning and the providers left out. The **recommended** provider is the cheapest right-sized total among providers with EU or UK locations whose catalogue holds the largest single workload; a provider that cannot hold it is shown as "no server fits" rather than with a misleadingly low total.

## 5. Everything else that costs money

| Line | How |
|---|---|
| Object storage | Provider's S3-compatible storage for the object GB +25% growth, priced single-site for every provider so the comparison is like for like (cloud standard tiers span zones). Providers without S3-compatible storage in Europe (Cherry Servers, phoenixNAP, Teraswitch) are priced at OVHcloud's rate as a stand-in, and flagged (`object_storage_fallback` in the catalogue). Archive tiers stay in the cloud (already cheaper per TB). |
| Off-site backup | `backup_factor` (2.0) × primary data (disks + database storage) in the provider's object storage, ideally another location. Replaces snapshots and backup services. |
| Traffic | Hetzner, OVHcloud and Scaleway include unmetered traffic on the standard port. Others include an allowance per server (Cherry Servers 100 TB, Leaseweb 30 TB, Latitude.sh 20 TB, Vultr 10 TB plus 2 TB per account) or a pool shared by the account (phoenixNAP 15 TB per location, Teraswitch 150 TB per region group, taken cautiously as one pool), with egress above it at the provider's published package or overage rate. If average egress × 3 (peak) exceeds 60% of total port capacity, 10G uplinks are added where priced. |
| Load balancers | Two of the provider's load balancers if the bill had any; otherwise HAProxy/keepalived on the cluster. |
| Per-server extras | e.g. Hetzner's €1.70/mo IPv4 per server, Scaleway's €19.99/mo private network per server; fixed extras such as phoenixNAP's /29 IPv4 block. |
| Retained cloud services | Today's cost of every retained line, unchanged. |
| Operations effort | Incremental engineering time for running hardware-backed infrastructure: `0.10 + 0.02 × servers + 0.05 × self-run data engines + 0.05 if shared storage + 0.05 if Kubernetes` FTE, at least 0.25 and at most `0.5 + 0.02 × servers`, at €110,000 a year fully loaded (Ireland, senior platform engineer). The report also shows the saving with a full extra engineer. Override with `--ops-fte` when the person knows better. |
| Contingency | 10% of bare-metal infrastructure. |
| One-offs | Setup fees; running both estates for 2 months (`parallel_run_months`). Not priced and stated as such: migration engineering (Exit Plan territory), remaining RI/Savings Plan terms, committed-spend shortfalls (EDP, MACC, CUD). Data transfer out is shown at list price, with the hyperscalers' conditional exit waivers and the EU Data Act's ban on switching charges from 12 January 2027 (at-cost egress for parallel running is still allowed). |
| Resilience | One provider region, spread across its data-centre buildings where possible, with off-site backups in a second location. Database replicas and app-pool spares sit on separate servers. A warm standby site is not priced; add it as a second estimate if the client needs regional DR. |

## 6. The baseline and the saving

- **Baseline** = every bill line except tax and credits, converted to EUR at the catalogue's ECB rate (USD, GBP and a dozen other currencies, one dated rate) and normalised to a 730-hour month. Providers that price in US dollars are converted at the same rate; the original figure is kept in the catalogue as `list_price`. Negotiated discounts (EDP, CUDs, SUDs) stay in: they are real. Commitment fees are either amortised into usage (when the export has effective-cost columns) or kept as a commitment line.
- **Saving** = baseline − bare-metal total. Reported with and without the operations line.
- Retained lines appear on both sides, so they don't change the saving but do keep percentages honest.

## 7. Confidence

| Grade | When |
|---|---|
| medium-high | ≥80% of sized spend comes from usage quantities, ≥60% of spend is sized, and the bill has usage detail |
| medium | ≥50% of sized spend from usage quantities |
| low | otherwise (typically PDF invoices), and any bill covering under 0.9 months |

There is no "high": a bill never shows utilisation, architecture or growth plans. That is what a Teardown is for.

## 8. Sanity checklist

Run through this on `plan.json` before writing the report. Fix the cause, don't hide the symptom.

- [ ] `baseline_eur_month` is within a few percent of the bill's pre-tax total (after FX and month normalisation).
- [ ] Unclassified ("other") spend under 5%, or reviewed and dispositioned.
- [ ] Every pool's `utilisation` is plausible: CPU and RAM 30–80%, NVMe under 75%. A pool at 10% CPU and 70% RAM is fine (memory-bound); one at 5% of everything means a minimum-node floor is driving cost, which matters for small estates.
- [ ] `binding_constraint` makes sense for the workload (databases: RAM or NVMe; web tier: CPU).
- [ ] No flag says the largest workload didn't fit, and no provider shows "no server fits". If one does, a bigger configuration is needed in the catalogue (OVHcloud's base configurations stop at 128 GB RAM).
- [ ] GPU matches are sensible (memory and class), or unmatched GPUs are explained.
- [ ] Right-sized ≤ like-for-like. If not, check the per-pool choices: the optimiser can legitimately pick a layout with fewer, larger servers in one scenario.
- [ ] The operations line is plausible for this organisation. If they already run Kubernetes on VMs with their own team, consider `--ops-fte` lower and say so.
- [ ] Catalogue date and FX date are stated; unverified prices (`verified: false`) are not the main driver of the headline.
- [ ] If an opt-in provider is featured, its caveat (currency, VAT basis, stock, object-storage stand-in) is in the summary you give.
