# estate.json format

`ingest.py` writes this file; you can also write it by hand (for PDF invoices) or edit it (to fix a classification). `estimate.py` only needs the fields marked required.

```json
{
  "schema": "counterweight.estate/1",
  "meta": {
    "input_kind": ["invoice_pdf"],
    "period_start": "2026-09-01",
    "period_end": "2026-10-01",
    "period_hours": 720,
    "months_covered": 0.986,
    "currency": "USD",
    "total_cost_ex_tax_ex_credits": 41250.00,
    "credits": -500.00,
    "detail_level": "service",
    "unclassified_top": []
  },
  "items": [
    {"category": "compute", "service": "Amazon EC2", "cloud": "aws",
     "instance_type": "m6i.2xlarge", "usage_hours": 8640, "os": "linux", "cost": 3697.92},
    {"category": "database", "service": "Amazon RDS", "cloud": "aws", "engine": "postgres",
     "instance_type": "db.r6g.xlarge", "usage_hours": 720, "multi_az": true, "cost": 760.00},
    {"category": "database", "service": "Amazon RDS", "cloud": "aws", "cost": 1900.00},
    {"category": "block_storage", "service": "Amazon EC2", "storage_gb_month": 12000, "cost": 1056.00},
    {"category": "egress", "service": "Data Transfer", "transfer_gb": 30000, "cost": 2400.00},
    {"category": "support", "service": "AWS Support (Business)", "cost": 1100.00}
  ]
}
```

## meta

| Field | Required | Notes |
|---|---|---|
| `period_hours` | yes | Hours in the billing period (730 for an average month). Used to turn hours into average running instances. |
| `months_covered` | yes | `period_hours / 730`. Costs are divided by this to get a monthly figure. |
| `currency` | yes | ISO code of the bill: EUR, USD, GBP, or another currency the catalogue's `fx` block lists (CHF, JPY, CNY, SEK...). |
| `input_kind` | no | `aws`, `azure`, `gcp`, `oci`, `ibm`, `focus`, `summary`, `invoice_pdf`, `manual`. Used in the report wording. |
| `period_start`, `period_end` | no | ISO dates; end may be exclusive. |
| `credits` | no | Negative; reported, not counted. |
| `detail_level` | no | `usage` or `service`. |
| `unclassified_top` | no | Lines the parser couldn't place; review them. |

## items

| Field | Required | Notes |
|---|---|---|
| `category` | yes | One of the categories below. Decides what happens to the line. |
| `cost` | yes | Cost for the whole period, in `meta.currency`, before tax. Negative for discounts and credits. |
| `service` | no | Provider service name. Dispositions can target it by name. |
| `cloud` | no | `aws`, `azure`, `gcp`, `oci`, `ibm`, `alibaba`; helps the instance decoder. |
| `instance_type` | no | e.g. `m6i.2xlarge`, `db.r6g.xlarge`, `Standard_D8s_v5`, `n2-standard-8`, `bx2-4x16` (IBM VPC), `ecs.g7.xlarge` (Alibaba). Decoded into vCPU/RAM/GPU. |
| `usage_hours` | no | Instance hours in the period (count × hours). |
| `vcpu_hours`, `ram_gb_hours` | no | Use instead of instance type when the bill gives cores and memory (GCP, OCI, Fargate, Azure vCore, IBM Cloud Databases). OCI OCPUs are converted to vCPUs first: 2 per OCPU on x86 and Ampere A2/A4, 1 on Ampere A1. |
| `series` | no | Machine series when there is no instance type: GCP (`n2`, `t2a`) or OCI (`oci-e4`, `oci-a1`). Sets SMT and the generation factor. |
| `gpu_hours`, `gpu_model` | no | `gpu_model` from: T4, A10G, L4, L40S, V100, A100-40, A100-80, H100, H200, B200. |
| `storage_gb_month` | no | GB-months for the period (= average GB × months). |
| `transfer_gb` | no | GB transferred in the period. |
| `engine` | no | `postgres`, `mysql`, `sqlserver`, `oracle`, `redis`, `opensearch`, `kafka`... |
| `os` | no | `linux`, `windows`, `rhel`, `suse`. Windows and commercial engines are flagged for licences. |
| `multi_az` | no | Informational. |
| `replicas_included` | no | True when every replica is billed as its own line (Aurora readers, ElastiCache nodes). Stops the estimate adding replicas. |
| `standby` | no | True for a separately billed HA standby; its demand is skipped because the primary gets a replica anyway. |
| `pricing_model` | no | `on_demand`, `reserved`, `savings_plan`, `spot`, `committed`. |
| `disposition` | no | Force `migrate`, `retain`, `eliminate`, `baseline_only` or `exclude` for this line. |
| `cost_implied_ok` | no | On a usage-level estate, allow capacity to be implied from this line's cost (normally only service-level estates get that). |

A line with only `category` and `cost` is fine: compute-like categories are sized from cost using reference list prices and marked cost-implied.

## Categories and default dispositions

| Category | Default | Category | Default |
|---|---|---|---|
| compute | migrate | object_storage | migrate |
| container | migrate | archive_storage | retain |
| serverless | migrate | file_storage | migrate |
| gpu | migrate | backup | migrate (replaced by off-site backup) |
| kubernetes_control | migrate (control plane on the cluster) | egress | migrate (mostly included in bare-metal prices) |
| database | migrate | inter_az, nat, ip | eliminate |
| database_storage | migrate | load_balancer | migrate |
| cache, cache_storage | migrate | network_other (VPN, peering, private links) | retain |
| search, search_storage | migrate | cdn, dns, email, identity, security | retain |
| messaging | migrate if it has instance hours (MSK, MQ), else retain | analytics, nosql, ml_managed | retain |
| block_storage | migrate | monitoring | migrate (self-hosted stack) |
| support | eliminate | marketplace, other | retain |
| commitment | eliminate | discount | baseline_only |
| credit, tax | exclude | | |
