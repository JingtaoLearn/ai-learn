# finance — Legacy Investment Report Archive

Read-only compatibility service for historical investment-report URLs. New reports are no longer registered here; the single active navigation registry is `projects/content-hub`.

**URL**: https://finance.ai.jingtao.fun

## What it serves

- `/` → `dashboard.html` — tab-grouped aggregate dashboard (auto-refreshed on every `build_dashboard.py` run)
- `/<asset_class>/<owner>/<code>/report.html` — per-report full reports
  - `asset_class` ∈ `wealth` | `stock` | `insurance` | `fund` | `bond` | `crypto` | `other`
  - `owner` = bank/market/insurer slug, lowercase alnum + hyphen
  - `code` = product code, alnum
- `/<asset_class>/<owner>/<code>/card.json` — structured dashboard card data
- `/<asset_class>/<owner>/<code>/meta.json` — structured product metadata (per-skill schema)
- `/<asset_class>/<owner>/<code>/chart.svg` — charts
- `/<asset_class>/<owner>/<code>/pdfs/*.pdf` — official disclosure PDFs
- `/<asset_class>/<owner>/<code>/assets/*.{png,jpg,svg,webp}` — skill-specific images
- `/index.json` — machine-readable aggregate summary
- `/README.md` — archive documentation

Hidden from web (server-side raw data only):
- `data.json` / `nv_full.json` / `reports.json` — raw API dumps
- `*.py` — build/register scripts
- `share_url.txt` — back-pointers to share-hosting
- `.history/` — version-history snapshots (internal use)

## Data source

Read-only mount of `/home/jingtao/finance/reports/`. The existing dashboard and report paths stay online so bookmarks and Content Hub cards do not break. No Skill may call the retired `register_report.py` write path.

## Architecture

- **Read-only compatibility**: historical report and dashboard URLs remain stable.
- **No new writes**: all new financial HTML publishes to its canonical host and registers directly in the `investment-research` Content Hub category.
- **Retirement boundary**: this service may be removed only after every historical card has migrated away from `finance.ai.jingtao.fun`.

## Naming history

Originally deployed as `licai.ai.jingtao.fun` (理财 = wealth management) when the hub only covered bank wealth products. Renamed to `finance.ai.jingtao.fun` on 2026-06-13 once coverage expanded to all asset classes.

## Deploy / update

```bash
cd ~/ai-learn/projects/finance
docker compose down && docker compose up -d
```

Install the fail-fast tombstone at the former write entry point:

```bash
install -m 755 retired_register_report.py \
  /home/jingtao/finance/reports/register_report.py
```

First request after fresh deploy may take 5-30s while letsencrypt-companion issues the cert.

## Related

- **share-hosting** (`share.ai.jingtao.fun`): UUID-only file share for ad-hoc reports — complements this with unstructured paths
- **content-hub-registry** Hermes Skill: the only active registration contract
- **cn-bank-wealth-products / cn-stock-financial-analysis / insurance-brochure-analysis** Hermes Skills: publish canonical HTML and register directly in Content Hub
