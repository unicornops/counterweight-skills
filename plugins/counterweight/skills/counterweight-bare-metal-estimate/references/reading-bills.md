# Getting and reading a cloud bill

What the estimate can do depends on what the bill contains. Quantities (instance hours, GB-months, GB transferred) give resource-based sizing; cost alone gives cost-implied sizing with lower confidence.

| Input | Has quantities | Confidence | Time to get |
|---|---|---|---|
| FOCUS export (any cloud) | Yes | Highest | Hours (first delivery) |
| AWS CUR / CUR 2.0, Azure cost details, GCP billing export | Yes | Highest | Hours, or already set up |
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

## PDF invoices and screenshots

Read the document and build `estate.json` yourself (`estate-schema.md`):

1. Total before tax, currency, billing period.
2. One item per service line with its cost and the right `category`.
3. Wherever the invoice shows an instance type and hours, add `instance_type` and `usage_hours`; for storage, `storage_gb_month`; for data transfer, `transfer_gb`.
4. Set `meta.input_kind` to `["invoice_pdf"]` so the report describes the source correctly.

Expect "low" or "medium" confidence and tell the person that a usage export would sharpen the result.

## Several accounts or clouds

Pass all files to one `ingest.py` call; items from different clouds sit side by side. If the files are in different currencies, run them separately or convert first: the estimate assumes one bill currency.
