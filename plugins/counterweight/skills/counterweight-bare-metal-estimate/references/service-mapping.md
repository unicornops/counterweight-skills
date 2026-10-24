# What each managed service becomes on bare metal

Use this to answer "how would you replace X?" and to decide dispositions. Effort is the engineering work to move a typical production workload: **low** (days), **medium** (weeks), **high** (months, or redesign). Licences change; check the current licence of any open-source project before recommending it in writing. The equivalents are the same whichever cloud the bill is from, but the saving is not: OCI lists compute and egress well below AWS and Azure, so the same estate saves less there.

## Compute and platforms

| Cloud service | Bare-metal equivalent | Effort | Notes |
|---|---|---|---|
| EC2 / Azure VMs / Compute Engine / OCI Compute / IBM Virtual Server for VPC / Alibaba ECS | VMs on KVM (Proxmox, OpenStack, Harvester) or containers on Kubernetes directly on the hosts | low–medium | Most estates are better off containerising than recreating VMs one for one. |
| EKS / AKS / GKE / OKE / IBM Kubernetes Service and Red Hat OpenShift / Alibaba ACK | Kubernetes on the servers: Talos, k3s, RKE2 or kubeadm; Cluster API to manage | medium | Control plane runs on the cluster (3 members). Ingress via ingress-nginx/Traefik/Envoy Gateway; MetalLB or the provider's load balancer for service IPs. |
| Fargate / Cloud Run / App Service / Container Apps / IBM Code Engine / OKE virtual nodes | Plain Kubernetes deployments; Knative where scale-to-zero matters | low–medium | |
| Lambda / Azure Functions / Cloud Functions / OCI Functions / Alibaba Function Compute | Containers on Kubernetes; Knative or OpenFaaS for event-driven functions | medium | Event sources (S3 events, SQS triggers) need replacements; this is often the real work. |
| Auto Scaling groups | Cluster autoscaling within a fixed fleet; spare capacity planned in | low | Bare metal scales in hours (ordering), not seconds. Keep a buffer, or burst to cloud. |
| Spot / preemptible | Fixed capacity | — | Already discounted in the cloud; smaller saving. |

## Data services

| Cloud service | Bare-metal equivalent | Effort | Notes |
|---|---|---|---|
| RDS / Aurora / Cloud SQL / Azure Database / OCI Database with PostgreSQL / IBM Databases for PostgreSQL / ApsaraDB RDS and PolarDB (PostgreSQL) | PostgreSQL with CloudNativePG (on Kubernetes) or Patroni (on hosts); pgBackRest or Barman to object storage | medium | Local NVMe makes PostgreSQL markedly faster than network block storage. Aurora-specific features (fast clones, global database) need alternatives. |
| RDS / Cloud SQL / Azure / OCI MySQL HeatWave / IBM Databases for MySQL / ApsaraDB RDS (MySQL, MariaDB) | Percona Operator for MySQL, MySQL InnoDB Cluster, or Galera | medium | |
| SQL Server / Oracle (including OCI Autonomous Database, Base Database and Exadata) / Db2 | Same engine on bare metal with your own licences, or migrate to PostgreSQL | high | Licence-included cloud pricing hides a large cost. Flagged in the report. |
| ElastiCache / Memorystore / Azure Cache / OCI Cache / IBM Databases for Redis / ApsaraDB for Redis (Tair) | Valkey (Linux Foundation fork, BSD) or Redis | low | |
| OpenSearch Service / Elastic Cloud / OCI Search with OpenSearch / IBM Databases for Elasticsearch | OpenSearch (Apache 2.0) via its Kubernetes operator | medium | |
| MSK / Event Hubs / OCI Streaming / IBM Event Streams / Alibaba Kafka (Kafka API) | Apache Kafka with Strimzi | medium | |
| Amazon MQ / Service Bus | RabbitMQ (operator) or ActiveMQ | low–medium | |
| SQS / SNS / Pub/Sub / EventBridge | NATS JetStream or RabbitMQ | medium | Cheap to keep in the cloud at low volume; retained by default. |
| DynamoDB / Cosmos DB / Bigtable / Firestore / OCI NoSQL / Cloudant / Tablestore | ScyllaDB (Alternator offers a DynamoDB-compatible API; check its current licence), Cassandra, or redesign onto PostgreSQL | high | Retained by default: data model and access patterns usually need rework. |
| Redshift / BigQuery / Synapse / Snowflake / MaxCompute / AnalyticDB | ClickHouse, or Trino/Spark over Apache Iceberg on object storage | high | Retained by default; a strong phase-two candidate when the spend is large. |

## Storage

| Cloud service | Bare-metal equivalent | Effort | Notes |
|---|---|---|---|
| EBS / Managed Disks / Persistent Disk / OCI Block Volumes / IBM Block Storage for VPC / Alibaba cloud disks | Local NVMe for node disks; Longhorn or Rook-Ceph for replicated volumes | low–medium | Per TB, replicated NVMe on rented servers is not always cheaper than gp3. Savings come from compute and egress; storage is often close to break-even. |
| S3 / Blob / GCS / OCI Object Storage / IBM Cloud Object Storage / Alibaba OSS (standard tiers) | The provider's S3-compatible object storage; or self-hosted Ceph RGW, Garage or SeaweedFS on storage servers at hundreds of TB | low | Check S3 feature use (lifecycle rules, object lock, events). MinIO has scaled back its community edition since 2025; check its status before choosing it. |
| Glacier / Archive / Coldline / OCI Archive Storage / IBM Cold Vault and Archive | Keep where it is | — | Already cheaper per TB than any bare-metal option. |
| EFS / Azure Files / Filestore / OCI File Storage / Alibaba NAS | CephFS or NFS on the storage tier | medium | |
| Snapshots / AWS Backup / Azure Backup | Velero (Kubernetes), pgBackRest, restic/Kopia to object storage in a second location | low | Priced as off-site backup storage. |

## Network and edge

| Cloud service | Bare-metal equivalent | Effort | Notes |
|---|---|---|---|
| Internet egress | Included or near-free at Hetzner, OVHcloud and Scaleway; a generous allowance at the opt-in providers | — | Usually the single biggest saving for data-heavy products. OCI includes 10 TB a month free and charges little beyond it, so an OCI bill saves less here. |
| NAT gateways, cross-AZ traffic | Disappear (private network is flat-rate or free) | — | |
| ALB / NLB / Azure LB / Cloud LB / OCI Load Balancer / IBM Load Balancer for VPC / Alibaba SLB | Provider load balancer, or HAProxy/Envoy with keepalived/MetalLB | low | |
| VPC peering, Transit Gateway, PrivateLink, OCI FastConnect, IBM Direct Link | WireGuard (Tailscale/Netbird/Headscale) or provider private networking (vSwitch, vRack) | low–medium | Retained by default while hybrid connectivity is still needed. |
| CloudFront / Front Door / Cloud CDN / IBM Cloud Internet Services / Alibaba CDN | Keep a CDN: often a cheaper one (Bunny, Cloudflare, Fastly) | low | Retained by default at today's cost; swapping CDN is a separate saving. |
| Route 53 / Azure DNS / Cloud DNS | Keep as SaaS or move to another DNS provider | low | Retained. |
| WAF / Shield / DDoS | Provider DDoS protection plus a CDN-level WAF, or Coraza/ModSecurity | low–medium | Retained by default. |

## Operations and security

| Cloud service | Bare-metal equivalent | Effort | Notes |
|---|---|---|---|
| CloudWatch / Azure Monitor / Cloud Logging / OCI Monitoring and Logging / IBM Cloud Logs / Alibaba Log Service | Prometheus or VictoriaMetrics, Grafana, Loki, OpenTelemetry Collector | low–medium | Priced as 5% extra app-pool capacity. Log-heavy estates may need more storage. |
| GuardDuty / Defender / Security Command Center | Falco, Wazuh, CrowdSec; or a third-party CNAPP | medium | Retained at today's cost as a placeholder for a replacement product. |
| KMS / Key Vault / Secrets Manager / OCI Vault / IBM Key Protect | OpenBao (open-source fork of Vault), SOPS, External Secrets Operator | medium | |
| Cognito / Entra ID B2C / Identity Platform | Keycloak, Zitadel or Authentik | medium–high | Retained by default. |
| Support plans | Replaced by the operations-effort line (and, if wanted, Counterweight Steady State) | — | |

## Compliance notes

- **EU Data Act** (Regulation (EU) 2023/2854, Chapter VI): from 12 January 2027 providers may not charge switching charges, data egress for switching included, to EU customers. Until then the hyperscalers' own exit-egress waivers apply on request.
- **DORA** (financial entities): Article 28(8) requires exit strategies for ICT services supporting critical or important functions, and Commission Delegated Regulation (EU) 2024/1773 sets what the contractual arrangements must cover. AWS, Microsoft and Google Cloud are among the critical ICT third-party providers designated by the European Supervisory Authorities in November 2025. A costed bare-metal option is useful evidence for an exit plan; it is not the exit plan itself.
