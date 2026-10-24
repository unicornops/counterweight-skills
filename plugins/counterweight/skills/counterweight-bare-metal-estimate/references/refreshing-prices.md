# Refreshing the price catalogue

`assets/catalogue.json` is a snapshot. Bare-metal prices moved several times in 2026 (DRAM and NAND costs), so refresh it when it is more than about 60 days old, when a client is about to act on the numbers, or when asked. Only do this with web access; without it, use the snapshot and state its date.

## Rules

- **Official sources only** for prices that drive the headline: the provider's own pages, docs, price API or order catalogue. Third-party trackers may fill gaps but the entry gets `"verified": false`.
- **Never guess a price.** If a figure can't be found, leave the configuration out rather than estimate it.
- Record `source` (URL) on every server entry, the date you checked on the provider (`prices_checked`), and update `as_of` at the top to the oldest of those dates.
- Prices in another currency are converted at the catalogue's ECB rate (`fx`), and the original kept as `list_price` (`{"amount": 456, "currency": "USD", "per": "month"}`). Refresh `fx` whenever you convert.
- Prices are **EUR per month, excluding VAT**; setup fees separately. Note anything billed on top (IPv4, private network, uplinks) at provider level.
- Keep configs that are orderable now in normal stock. Promotions and limited stock go in with `"availability": "limited"` (skipped unless `allow_limited` is set).
- Write a copy (`catalogue.refreshed.json`) and pass it with `--catalogue`; replace the bundled file only when the person maintaining the skill asks.

## Where the prices are

| Provider | Where to look | Notes |
|---|---|---|
| Hetzner | docs.hetzner.com price-adjustment page (official table), product pages under hetzner.com/dedicated-rootserver, and the website price API the product pages load (`website-price-api.hetzner.com/api/v1/products/ROBOT_<id>`) | Product pages render prices with JavaScript, so a plain fetch shows blanks. Since 15 June 2026 new orders are fixed `-1/-2/-3` configurations. Prices exclude the €1.70/mo primary IPv4. |
| OVHcloud | ovhcloud.com/en-ie/bare-metal/prices/ and the public order catalogue (`eu.api.ovh.com/v1/order/catalog/formatted/baremetalServers?ovhSubsidiary=IE`, prices in units of 10⁻⁸ EUR) | Setup fee usually equals one month and is often waived by promotion. RAM upgrade prices are only in the configurator. EU datacentres share one price list. |
| Scaleway | The public product catalogue API (`api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=elastic_metal`, no key needed; also `object_storage`, `load_balancer`) and scaleway.com/en/pricing/ | Prices in EUR excluding VAT, identical in every zone; the `/fees` SKU is the one-month commitment fee (setup). Hardware is in `properties.hardware`. No stock data. |
| Cherry Servers | `api.cherryservers.com/v1/plans` (public; prices, specs and `stock_qty` per region) | EUR, `"taxed": false`. Check stock: most large plans showed none in Europe on 24 October 2026. Ignore sale prices. |
| Latitude.sh | latitude.sh/pricing (plan JSON in the page; `pricing.USD.month` per region) | USD. Monthly, not hourly, is the comparable price. Stock per region is in the same JSON. |
| phoenixNAP | `api.phoenixnap.com/billing/v1/products` (public) | USD excluding tax; use the `ONE_MONTH_RESERVATION` price for location `NLD`. Bandwidth packages are in the same feed. |
| Teraswitch | teraswitch.com/pricing and the configurator; the site sits behind a bot wall, so read it through a browser | USD; base price plus memory and drive options; EU and North America priced the same. |
| Vultr | `api.vultr.com/v2/plans-metal` (public) | USD excluding VAT. Only plans whose `locations` include a European site count as standard. |
| Exchange rates | ECB euro reference rates (`ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml`) | For each currency, `fx.<CUR>_EUR` = 1 / (units per EUR); update `fx.as_of` and `fx.source`. |

## Choosing providers

A provider goes in the catalogue when it has public list prices you can cite, locations in the EU or UK (a provider without them gets `"eu_uk_locations": false`, is flagged in the report and is never the recommended provider), API or automated provisioning, and a credible operator. It is **on by default** (`default_enabled`) only when its prices are official euro prices excluding VAT, it sells S3-compatible object storage and a load balancer, and its stock can carry a fleet of ten or more servers. Everything else is opt-in, like Leaseweb, with the reason in its `pricing_caveat`.

| Provider | Default | Why |
|---|---|---|
| Hetzner, OVHcloud | on | As before. |
| Scaleway (Elastic Metal) | on | Iliad-owned, Paris; official EUR prices from a public catalogue API; Paris, Amsterdam and Warsaw; S3 and load balancer priced. Stock is not published. |
| Leaseweb | opt-in | Shop data leaves VAT basis, setup fees and port speeds unstated. |
| Cherry Servers | opt-in | Lithuanian, EUR, public API; but thin stock in Europe, no UK site, no S3 object storage. |
| Latitude.sh | opt-in | Frankfurt, London, Amsterdam; USD prices with an ambiguous tax note; being acquired by Megaport. |
| phoenixNAP | opt-in | Amsterdam is its only European site; USD; object storage US-only. |
| Teraswitch | opt-in | US operator with European points of presence; USD, VAT unstated, European stock not visible without an account, no object storage yet. |
| Vultr | opt-in | Broad EU and UK coverage, USD; plans above 768 GB and all GPU servers had no deployable location. |

Checked and left out on 23–24 October 2026: **Equinix Metal** (retired); **servers.com** (hourly bare metal in Amsterdam only, egress per GB at an unpublished rate, monthly servers quote-only); **IONOS** (provisioning through an API not confirmed, 256 GB RAM ceiling, many prices are 12-month promotions with a one-year term); **DataPacket** (hardware priced well above the others, single-digit stock per site, and bandwidth billed separately on a peak-rate curve whose unit is not stated). Revisit them when that changes.

## Server entry

```json
{"model": "AX102-1", "cpu": "AMD Ryzen 9 7950X3D", "cores": 16, "threads": 32,
 "ram_gb": 128, "nvme_gb": 1920, "nvme_raw_gb": 3840, "hdd_tb": 0,
 "monthly_eur": 257.30, "setup_eur": 129.00, "role": ["general"],
 "availability": "standard", "source": "https://...", "verified": true}
```

- `threads`: hardware threads (cores × 2 with SMT; cores only for CPUs without SMT, such as Intel Core Ultra or Arm).
- `nvme_gb`: usable after RAID1 of the drive pairs; `nvme_raw_gb`: raw total. SATA SSDs count as NVMe here with a `note`.
- `role`: `general`, `storage` (HDD-heavy boxes; excluded from app/data pools), `gpu`.
- `gpu`: `{"model": "L40S", "name": "NVIDIA L40S", "mem_gb": 48, "count": 1}`. Use a model key from `GPU_RANK` in `estimate.py`, or add one with a sensible rank.

## Provider entry

`name`, `locations`, `default_enabled`, `eu_uk_locations`, `prices_checked`, `per_server_monthly_eur`, `fixed_monthly_eur` (optional, per estate), `traffic` (`port_gbps`, `included_tb_per_server` and/or `included_tb_pool` (shared by the account), both null for unmetered, `overage_eur_per_tb`, `uplink_10g_monthly_eur`), `object_storage` (`base_monthly_eur`, `included_tb`, `eur_per_tb_month`; null when the provider sells none in Europe, which uses `object_storage_fallback`), `backup_storage` (`eur_per_tb_month`, or null likewise), `load_balancer` (`monthly_eur`), `pricing_caveat` (one sentence shown in the report), `servers`.

## After refreshing

Run the bundled sample end to end (`assets/sample-aws-cur.csv`) and compare with the previous run. A swing of more than about 15% in the bare-metal total should be traceable to specific price changes; if it isn't, check the new entries.
