# Getting and reading a cloud bill

What the estimate can do depends on what the bill contains. Quantities (instance hours, GB-months, GB transferred) give resource-based sizing; cost alone gives cost-implied sizing with lower confidence.

| Input | Has quantities | Confidence | Time to get |
|---|---|---|---|
| FOCUS export (AWS, Azure, GCP, OCI, IBM Cloud) | Yes | Highest | Hours (first delivery) |
| AWS CUR / CUR 2.0, Azure cost details, GCP billing export, OCI cost report | Yes | Highest | Hours, or already set up |
| IBM Cloud usage CSV (Instances or Summary) | Mostly | High | Minutes |
| Alibaba Cloud FOCUS export (preview) | No | Low to medium | A few days (by invitation) |
| Console CSV by service and usage type | Partly | Medium | Minutes |
| PDF invoice | Rarely | Low to medium | Minutes |

Ask for **one complete month**, ideally the most recent. Three months smooths out spikes. Never ask for credentials or console access: an export file is enough.

Console menus move; describe these steps in general terms and let the person find the exact button.

## AWS

**Best: Data Exports (CUR 2.0 or FOCUS).** In Billing and Cost Management, create a data export (standard CUR 2.0, or FOCUS with AWS columns), CSV format, delivered to an S3 bucket the person owns. The first delivery can take up to a day and covers the current month; AWS Support can be asked about backfilling earlier months. Legacy CUR files (`lineItem/...` columns) work too.

**Fast: the Bills page CSV.** Billing → Bills → choose the month → download as CSV. Rows are by service, region and usage type with usage quantities, which is enough for instance-level sizing of EC2 and RDS. Pass it with `--format summary --cloud aws` if auto-detection doesn't recognise it, after checking which columns hold the service, the usage description, the quantity and the cost.

**Cost Explorer CSV:** service-level totals only. Fine for a first look; sizing will be cost-implied.

**PDF invoice:** lists charges by service; the detailed bill in the console adds usage per instance type ("$0.192 per On Demand Linux m5.xlarge Instance Hour", 744 Hrs). Copy those lines into `estate.json` by hand (see `estate-schema.md`).

**Things to know when reading AWS bills**

- *Amortised vs unblended:* CUR rows with `reservation_effective_cost` / `savings_plan_savings_plan_effective_cost` let the script spread Reserved Instance and Savings Plan fees across the usage they cover. Without them it uses unblended cost and keeps RI/SP fees as "commitment" lines.
- *EDP / private pricing:* negotiated discounts appear as `EdpDiscount` / `PrivateRateDiscount` lines (negative). They are part of the real baseline.
- *Credits* (Activate, MAP, promotional) are excluded from the baseline because they end.
- *Multi-AZ RDS* bills one instance line at double rate; the estimate gives every database a replica on bare metal anyway.
- *Graviton* (`m7g`, `c6g`, `r8g`...) vCPUs are whole cores; the decoder counts them as 1.4 x86 threads.

## Azure

**Best: Cost Management export.** Cost Management → Exports → cost and usage details, **amortised** (or FOCUS), CSV, to a storage account. Or Cost analysis → Download → usage details for a single month.

**Things to know**

- `AdditionalInfo` carries the VM size (`ServiceType: Standard_D8s_v5`); the script decodes it.
- Managed disks bill per disk tier (`P30 LRS Disk` = 1 TiB); the script converts tiers to GB.
- Azure SQL and PostgreSQL/MySQL Flexible Server bill **vCore hours**, which map directly to vCPU.
- HA standbys appear as their own line; the estimate does not double-count them.
- `UnusedReservation` rows are wasted commitment: they vanish on bare metal.
- Windows and SQL Server licences are included in the price of some meters; on bare metal they are not (flagged in the report).

## Google Cloud

**Best: billing export to BigQuery** (detailed usage cost), then query one month and save as CSV. The export only holds data from when it was enabled.

**Fast: Billing → Reports / Cost table → download CSV.** SKU-level rows with usage, available for past months.

**Things to know**

- Compute Engine bills **cores and RAM separately per series** ("N2 Instance Core running in Belgium", "N2 Instance Ram ..."), so vCPU and GB come straight from the SKUs.
- Sustained- and committed-use discounts are credits in the export; the script keeps them in the baseline (they're real), and excludes promotional and free-trial credits.
- Cloud SQL "Regional" SKUs are HA instances.

## Oracle Cloud (OCI)

**Best: Cost and Usage Reports.** Billing and Cost Management → Cost and Usage Reports. OCI writes two kinds to an Oracle-owned bucket, both CSV (gzipped), each file covering a few hours of usage: **cost reports** in OCI's own format (`lineItem/...`, `product/service`, `cost/myCost` columns) and **FOCUS reports** (`BilledCost`, `ServiceName`...), filed by year, month and day. Download every file for one month and pass them all to `ingest.py` in one call; it detects either format. Reports are kept for a year. The older usage reports were withdrawn in 2025 and are not needed.

**Fast: Cost Analysis → download as CSV.** Service-level totals; pass with `--format summary --cloud oci`. Sizing will be cost-implied.

**Things to know**

- *OCPUs:* flexible shapes bill **OCPU-hours and memory GB-hours separately** per series ("Standard - E4", "Standard - E4 - Memory"), so there is no shape name to decode. On x86 one OCPU is two vCPUs; on Ampere A1 one OCPU is one Arm core, on A2 and A4 two cores. The script converts accordingly.
- *Service codes:* `product/service` holds codes such as `COMPUTE`, `BLOCK_STORAGE`, `OBJECTSTORE`, `NETWORK`, `ORALB` (load balancers), `OKE_CONTROL_PLANE`, `ODSP` (Database with PostgreSQL), `OPENSEARCH`, `FILESTORAGESERVICE`. Services not in that list are classified from the SKU name in `product/description`.
- *Block volume performance units* (VPUs) are billed per GB-month alongside the storage; they are a performance fee, not more disk.
- *ECPUs* (Autonomous Database, MySQL HeatWave, Base Database) are an abstract unit; those lines are sized from their cost at list prices and marked cost-implied.
- *Universal Credits:* `cost/myCost` is already at your contracted unit price, overage included. Corrections (`lineItem/isCorrection`) amend earlier lines and are kept.
- OCI's first 10 TB of internet egress each month is free, and its compute is priced well below the other hyperscalers. Expect a smaller saving than from an AWS or Azure bill of the same size, and often none.

## IBM Cloud

**Best: the usage CSV.** Manage → Billing and usage → Usage → Export → CSV, choosing **Instances** (one row per instance and metric) for the month. The **Summary** export works too: its Account Resource Usage section has the same metrics aggregated per plan, and its Support Summary adds the support charge. Accounts can also deliver these daily to a Cloud Object Storage bucket. IBM's FOCUS export (Export → FOCUS) is read as FOCUS, but IBM does not document its unit values, so it sizes from cost.

**Things to know**

- The CSV starts with an Account Metadata block (owner, **Billing Month**, currency rate, version); the script reads the billing month from it and finds the usage table below. The version depends on the month: since January 2026 (v1.4.1) a **Profile** column carries the VPC instance profile.
- *Profiles* decode directly: `bx2-4x16` is balanced, x86, generation 2, 4 vCPU and 16 GiB. Without the Profile column, worker flavours in the plan name (`16x64-shared-instance` for Kubernetes Service) are used; otherwise compute lines are sized from cost.
- *Cost* is the pre-tax cost after discounts (`Usage Cost` in versions before 1.2). Rows marked **Non Chargeable** are informational and skipped.
- *Classic infrastructure* rows have unit `CHARGE`: the quantity is money, not hours. Cores and RAM come from the metric name (`GUEST_CORES_4`, `RAM_8_GB`) and are taken as running all month.
- *Databases for PostgreSQL / MySQL / Redis* bill every member's cores (`VIRTUAL_PROCESSOR_CORES`), RAM and disk (`GIGABYTE_MONTHS_RAM`, `GIGABYTE_MONTHS_DISK`), so replicas are already in the quantities.
- `WINDOWS_VCPU_HOURS` and similar OS lines are licences, kept at cost and flagged. Cloud Object Storage **Cold Vault** counts as archive storage.
- Subscription and offer credits appear only in the Summary export and are not counted.

## Alibaba Cloud

**Best: the FOCUS export** (Expenses and Costs → bill subscriptions, "Standard Bill FOCUS"), currently an invitation-only preview. Alibaba documents that its usage quantity, unit and SKU columns are always empty, so the estimate sizes everything from cost, like an invoice, and the confidence is low. Times are UTC.

**Not supported:** the console Bill Details export and the OSS bill subscriptions (Billing Item Details, Instance Details, Split Bill). Alibaba documents their fields but not the exact header row, and its pages disagree on the name of the payable-amount column, so the script does not guess. If that is all the person has, read it like an invoice: one cost line per product with the right category, plus `instance_type` and `usage_hours` where an instance specification such as `ecs.g7.xlarge` and a duration are shown (`specs.py` decodes `ecs.` types in the c, g and r families). Use the pre-tax payable amount, after discounts and coupons.

## PDF invoices and screenshots

Read the document and build `estate.json` yourself (`estate-schema.md`):

1. Total before tax, currency, billing period.
2. One item per service line with its cost and the right `category`.
3. Wherever the invoice shows an instance type and hours, add `instance_type` and `usage_hours`; for storage, `storage_gb_month`; for data transfer, `transfer_gb`.
4. Set `meta.input_kind` to `["invoice_pdf"]` so the report describes the source correctly.

Expect "low" or "medium" confidence and tell the person that a usage export would sharpen the result.

## Several accounts or clouds

Pass all files to one `ingest.py` call; items from different clouds sit side by side. If the files are in different currencies, run them separately or convert first: the estimate assumes one bill currency. Bills in currencies other than EUR, USD and GBP are converted at the catalogue's ECB rate when it lists one (`fx`); otherwise convert the amounts to euros first and pass `--currency EUR`.
