# finance — Investment Research Hub

Dedicated subdomain for Jingtao's investment-research archive across all asset classes (理财 / 股票 / 保险 / 基金 / 债券 / 加密 / ...).

**Primary URL**: https://finance.ai.jingtao.fun
**Legacy URL** (301-redirects to primary): https://licai.ai.jingtao.fun

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

Read-only mount of `/home/jingtao/finance/reports/`. The dashboard regenerates on every `register_report.py` call (which any analysis skill invokes after publishing). nginx serves the freshly-written file with no restart needed.

## Architecture

- **Concurrency-safe**: each report lives in its own dir, dashboard build is pure scan. Multiple skills can register simultaneously.
- **URL stable**: nginx whitelist regex on path means the URL contract is enforced — re-running an analysis with the same `(asset_class, owner, code)` reuses the URL.
- **Overwrite semantics**: re-analysis replaces in place (typically what you want — show latest verdict). `--keep-history` flag preserves old version under `.history/`.

## Naming history

Originally deployed as `licai.ai.jingtao.fun` (理财 = wealth management) when the hub only covered bank wealth products. Renamed to `finance.ai.jingtao.fun` on 2026-06-13 once coverage expanded to all asset classes. The legacy hostname does a 301 permanent redirect.

## Deploy / update

```bash
cd ~/ai-learn/projects/finance
docker compose down && docker compose up -d
```

First request after fresh deploy may take 5-30s while letsencrypt-companion issues the SAN cert (covers both `finance.*` and `licai.*`).

## Related

- **share-hosting** (`share.ai.jingtao.fun`): UUID-only file share for ad-hoc reports — complements this with unstructured paths
- **investment-research-registry** Hermes skill: the registration contract that all analysis skills follow
- **cn-bank-wealth-products / cn-stock-financial-analysis / insurance-brochure-analysis** Hermes skills: analysis skills that populate this dashboard
