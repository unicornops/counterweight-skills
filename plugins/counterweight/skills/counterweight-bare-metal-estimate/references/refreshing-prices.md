# Refreshing the price catalogue

`assets/catalogue.json` is a snapshot. Bare-metal prices moved several times in 2026 (DRAM and NAND costs), so refresh it when it is more than about 60 days old, when a client is about to act on the numbers, or when asked. Only do this with web access; without it, use the snapshot and state its date.

## Rules

- **Official sources only** for prices that drive the headline: the provider's own pages, docs, price API or order catalogue. Third-party trackers may fill gaps but the entry gets `"verified": false`.
- **Never guess a price.** If a figure can't be found, leave the configuration out rather than estimate it.
- Record `source` (URL) on every server entry and update `as_of` at the top.
- Prices are **EUR per month, excluding VAT**; setup fees separately. Note anything billed on top (IPv4, private network, uplinks) at provider level.
- Keep configs that are orderable now in normal stock. Promotions and limited stock go in with `"availability": "limited"` (skipped unless `allow_limited` is set).
- Write a copy (`catalogue.refreshed.json`) and pass it with `--catalogue`; replace the bundled file only when the person maintaining the skill asks.

## Where the prices are

| Provider | Where to look | Notes |
|---|---|---|
| Hetzner | docs.hetzner.com price-adjustment page (official table), product pages under hetzner.com/dedicated-rootserver, and the website price API the product pages load (`website-price-api.hetzner.com/api/v1/products/ROBOT_<id>`) | Product pages render prices with JavaScript, so a plain fetch shows blanks. Since 15 June 2026 new orders are fixed `-1/-2/-3` configurations. Prices exclude the €1.70/mo primary IPv4. |
| OVHcloud | ovhcloud.com/en-ie/bare-metal/prices/ and the public order catalogue (`eu.api.ovh.com/v1/order/catalog/formatted/baremetalServers?ovhSubsidiary=IE`, prices in units of 10⁻⁸ EUR) | Setup fee usually equals one month and is often waived by promotion. RAM upgrade prices are only in the configurator. EU datacentres share one price list. |
| Leaseweb | leaseweb.com and its shop API (`shop-api.leaseweb.com/occ/v2/leaseweb-spa/products/...`) | VAT basis, setup fees and port speeds are not stated in the shop data; most stock is SATA SSD. Kept opt-in until verified. |
| Exchange rates | ECB euro reference rates (ecb.europa.eu) | Update `fx.USD_EUR` = 1 / (USD per EUR), `fx.GBP_EUR` likewise, and `fx.as_of`. |

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

`name`, `locations`, `default_enabled`, `per_server_monthly_eur`, `traffic` (`port_gbps`, `included_tb_per_server` or null for unmetered, `overage_eur_per_tb`, `uplink_10g_monthly_eur`), `object_storage` (`base_monthly_eur`, `included_tb`, `eur_per_tb_month`), `backup_storage` (`eur_per_tb_month`), `load_balancer` (`monthly_eur`), `pricing_caveat` (one sentence shown in the report), `servers`.

## After refreshing

Run the bundled sample end to end (`assets/sample-aws-cur.csv`) and compare with the previous run. A swing of more than about 15% in the bare-metal total should be traceable to specific price changes; if it isn't, check the new entries.
