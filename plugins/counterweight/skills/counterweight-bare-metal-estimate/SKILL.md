---
name: counterweight-bare-metal-estimate
description: Turn an existing AWS, Azure, Google Cloud, Oracle Cloud (OCI), IBM Cloud or Alibaba Cloud bill into an indicative bare-metal plan from Counterweight - which dedicated servers (Hetzner, OVHcloud, Scaleway, and on request Leaseweb, Cherry Servers, Latitude.sh, phoenixNAP, Teraswitch, Vultr) would replace the workloads, what they would cost each month, the likely saving after operations effort, one-off costs, and what should stay in the cloud. Use it whenever someone shares a cloud invoice, Cost and Usage Report, Azure cost export, GCP billing export, OCI cost report, IBM Cloud usage CSV or FOCUS file and asks what bare metal or dedicated servers would cost, whether to repatriate or leave the cloud, how much they could save on their cloud bill, or wants an exit or migration estimate, even if they never say "bare metal". Produces a client-ready report in Markdown and branded HTML. Runs offline from a dated price catalogue; refreshes prices when web access is available.
license: Apache-2.0 (code and docs); the Counterweight name and logo are not licensed. See LICENSE.txt
compatibility: Needs Python 3.9+ (standard library only) and the ability to run scripts. Web access is optional, used only to refresh prices and exchange rates.
metadata:
  author: "Counterweight (Unicorn Operations Ltd)"
  version: "1.1.0" # x-release-please-version
  website: "https://counterweight.ie"
  catalogue-as-of: "2026-10-19"
---

# Counterweight bare-metal estimate

This skill gives someone a first, honest answer to "what would our cloud estate cost on bare metal?" from the bill they already have. It is the AI-native version of an initial consultation with Counterweight: the person keeps their data, gets real numbers in minutes, and sees exactly what a paid Teardown would firm up.

The output is an **indicative estimate**, a range not a quote. Its value is that it is specific (named server models, counts, monthly euros) and candid about what it cannot know from a bill. Treat credibility as the product: an estimate that overstates savings costs Counterweight more than one that understates them.

## Ground rules

- **Billing data stays here.** Work only on files the person gives you. Never upload a bill, paste it into a web search, or ask for cloud credentials or access keys. If they offer credentials, decline: a bill export is all this needs.
- **Show the range and the operations line.** Always present like-for-like and right-sized figures, and always show the saving both with and without the operations-effort allowance. Never quote only the best case.
- **Say when bare metal doesn't pay.** Small or spiky estates, heavy use of proprietary managed services, or large spot discounts can mean little or no saving. Say so plainly; that answer builds trust.
- **Don't invent prices.** Use the bundled catalogue or prices you have just verified on the provider's own site. State the catalogue date in the report.
- **No legal or financial advice.** Regulatory notes (EU Data Act, DORA) are context, not advice.

## Workflow

### 1. Get the bill

Ask what they can share, and accept whatever they have. In order of usefulness:

1. A **FOCUS** export, **AWS Cost and Usage Report** (legacy or CUR 2.0), **Azure cost details export** (amortised preferred), **Google Cloud billing export**, **OCI cost report** (or OCI's FOCUS report), or **IBM Cloud usage CSV** ("Instances", or the "Summary" export) for one full month (three months is better). These carry usage quantities, so sizing is resource-based. Alibaba Cloud's FOCUS export is accepted too, but it carries no quantities, so it sizes like an invoice.
2. A console **CSV download** of the bill by service and usage type (AWS Bills page, Azure Cost analysis, GCP Cost table, OCI Cost Analysis). Partly quantity-based.
3. A **PDF invoice**. Service-level only; sizing falls back to cost-implied capacity and the confidence drops.

If they don't know how to get these, read `references/reading-bills.md` and give them the short version for their cloud. If they want an answer now, start with whatever they have and say what a better export would add.

Also ask, in one message and only if not already clear: the company or client name for the report, whether they are a financial entity under DORA, and whether they know average or peak CPU utilisation. All optional; don't block on them.

### 2. Normalise the bill

```bash
python3 scripts/ingest.py BILL.csv [MORE.csv ...] -o estate.json
```

It detects the format (AWS, Azure, GCP, OCI, IBM Cloud, FOCUS from any cloud, or a service summary), classifies every line into a category, decodes instance types and OCI's OCPU series into vCPU/RAM/GPU, and prints a summary (period, total, cost by category, unclassified spend). Run with `--help` for options (`--format`, `--cloud` for summary files, `--period-hours`, `--currency`).

Alibaba Cloud's own bill-details CSVs are not parsed: Alibaba does not document their exact headers. Ask for its FOCUS export, or treat the bill like a PDF invoice (below).

Then check the summary:

- **Total** should match the bill's pre-tax total less credits, within a few percent. Amortised exports spread upfront Reserved Instance and Savings Plan fees across the month, so they won't match an invoice that billed an upfront fee; that's expected. Otherwise find out why before going further (wrong cost column, a second currency, a partial month).
- **Locale:** the script reports when it detects decimal commas or day-first dates. If it says the date order is ambiguous and the period looks wrong, pass `--period-hours`.
- **Unclassified spend over 5%:** look at `meta.unclassified_top` in `estate.json` and either set a disposition (step 4) or add the line's category by editing the item.
- **Period:** `months_covered` near 1.0 for a monthly bill. Partial months skew averages.

**PDF invoices or anything the parser can't read:** read the document yourself and write `estate.json` by hand following `references/estate-schema.md`. Put instance types and hours in where the invoice shows them (AWS invoices often list hours per instance type); everything else as cost-only lines with the right category.

### 3. Check prices are current

The catalogue at `assets/catalogue.json` is dated (`as_of`). If you have web access and the catalogue is more than about 60 days old, or the person asks for current prices, refresh it following `references/refreshing-prices.md`. Without web access, use it as it is and say the date in your summary.

Exchange rate: bills in USD, GBP and the other currencies in the catalogue's `fx` block (CHF, the Nordic and Central European currencies, JPY, CNY, INR, SGD, AUD, CAD) are converted at its dated ECB rate. With web access, look up today's ECB reference rate and pass `--fx-usd-eur` (EUR per USD). Providers that price in US dollars are converted at the same catalogue rate.

### 4. Estimate

```bash
python3 scripts/estimate.py estate.json -o plan.json [--providers default,cherryservers | all]
  [--cpu-util 0.5] [--ops-fte 0.5] [--assumptions assumptions.json]
```

What it does and every default is in `references/sizing-method.md`. Read it before changing assumptions or explaining the method in depth. The short version:

- Each line gets a **disposition**: migrate (sized onto servers), retain (kept at today's cost: CDN, DNS, archive tiers, data warehouses, proprietary NoSQL, managed AI, licences, accelerators with no bare-metal match), eliminate (NAT, cross-zone traffic, support plans, unused commitments), or exclude (tax, credits).
- Migrating demand is pooled (application, data services, GPU, shared storage), headroom and redundancy are added, and the cheapest server model per pool is chosen per provider.
- Two scenarios: **like-for-like** (a thread per cloud vCPU, same RAM, provisioned disk) and **right-sized** (utilisation and CPU-generation factors, disk actually used).
- Costs add object storage, off-site backup, traffic, load balancers, IPv4, retained cloud services, an **operations-effort** line (at least a quarter of an engineer), and 10% contingency. The design is single-region with backups in a second location; a warm DR site is not priced.
- Bills covering less than a week are refused (`--allow-partial` overrides); under 0.9 months the confidence is graded low.

Use what the person told you:

- Known utilisation: `--cpu-util` = p95 CPU / 100 × 1.25; average only: average / 100 × 2. Keep it between 0.3 and 1.0.
- Their own platform team will absorb the work: pass `--ops-fte` with their estimate, or `0` with a clear note.
- Things they want kept in the cloud, or moved that default to retained: an `assumptions.json` with `"dispositions": {"cdn": "migrate", "AmazonDynamoDB": "retain"}` (keys are categories or service names). See `assets/assumptions.example.json`.
- Providers: Hetzner, OVHcloud and Scaleway are priced by default. Leaseweb, Cherry Servers, Latitude.sh, phoenixNAP, Teraswitch and Vultr are opt-in (`--providers default,teraswitch`, or `all`): each has a gap, listed in its `pricing_caveat` and in `references/refreshing-prices.md`, that stops it driving a headline unasked. The recommended provider is the cheapest right-sized total among those with EU or UK locations whose servers can hold the largest workload.
- Data residency: pass `--providers` to limit to providers with suitable locations. Their locations are in the report.

**Sanity-check plan.json before reporting** (the checklist in `references/sizing-method.md`): server utilisation figures, the binding constraint per pool, whether the largest single instance fit, GPU matches, and the flags. If a number looks wrong, it probably is: find out why rather than smoothing it over.

### 5. Report

```bash
python3 scripts/report.py plan.json -o report.md --html report.html --client "Acme Ltd" [--sector financial]
```

- `report.md` is portable: paste it anywhere, any AI tool can render it.
- `report.html` is a single-file page in the Counterweight brand (Paper theme), with Archivo and IBM Plex Mono embedded: it makes no requests to other sites and looks the same offline. Publish or share it where the environment allows (as an artifact, a file to download, or print to PDF).
- `--provider` features a provider other than the cheapest.

Deliver both files. Then, in conversation, give the person:

1. The headline: today's monthly bill, the bare-metal range, the saving range with and without operations effort.
2. The two or three biggest drivers of the saving (typically egress, compute, managed-database markup) and the biggest thing kept in the cloud.
3. The confidence grade and the one thing that would most improve it (usually a usage-level export or measured utilisation).
4. The next step: a **Teardown** (two weeks, run by their own team with Counterweight's skills on read-only exports, measured not assumed, a costed plan with a signed review) via hello@counterweight.ie or counterweight.ie.

Follow `references/report-voice.md` for wording: numbers before adjectives, euro figures with periods, no hype words.

## Answering follow-ups

- "Why this server?" → `plan.json` → `providers.<p>.scenarios.<s>.servers[]` has the model, count, binding constraint and utilisation.
- "What if we keep X / move Y?" → change the disposition and rerun steps 4–5. It takes seconds.
- "How do you replace RDS/EKS/S3/...?" → `references/service-mapping.md`.
- "Is this a quote?" → No. It is an estimate from billing data alone; provider prices must be confirmed at order, and a Teardown replaces the assumptions with measurements.

## Files

| Path | What it is |
|---|---|
| `scripts/ingest.py` | Bill → `estate.json` (AWS CUR/CUR 2.0, Azure, GCP, OCI, IBM Cloud, FOCUS, service summaries) |
| `scripts/specs.py` | Instance-type decoder for AWS, Azure, GCP, IBM Cloud VPC and Alibaba ECS, plus OCI's OCPU series (also a CLI: `python3 scripts/specs.py m6i.2xlarge`) |
| `scripts/estimate.py` | `estate.json` + catalogue → `plan.json` (sizing and pricing) |
| `scripts/report.py` | `plan.json` → `report.md` and branded `report.html` |
| `assets/catalogue.json` | Dated bare-metal price catalogue and exchange rates |
| `assets/assumptions.example.json` | Example overrides |
| `assets/sample-aws-cur.csv` | A small synthetic AWS bill to demo the skill end to end |
| `assets/fonts/` | Archivo and IBM Plex Mono (SIL Open Font License 1.1), embedded in `report.html` |
| `references/reading-bills.md` | How to export a bill from each cloud; reading PDF invoices |
| `references/estate-schema.md` | The `estate.json` format, for hand-built estates |
| `references/sizing-method.md` | The method, every default and why, and the sanity checklist |
| `references/service-mapping.md` | What each managed service becomes on bare metal, and the effort |
| `references/refreshing-prices.md` | How to update the catalogue safely |
| `references/report-voice.md` | How to word results in Counterweight's voice |

All scripts are standard-library Python, take `--help`, write JSON or Markdown to files, print a short summary to stderr, and never touch the network.

To demo the skill without a real bill: `python3 scripts/ingest.py assets/sample-aws-cur.csv -o estate.json` and carry on from step 3.
