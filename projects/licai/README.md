# licai — Wealth-Products Dashboard

Dedicated subdomain for Jingtao's wealth-management research archive.

**URL**: https://licai.ai.jingtao.fun

## What it serves

- `/` → `dashboard.html` — the aggregate dashboard (auto-refreshed on every `build_dashboard.py` run)
- `/<bank>/<code>/report.html` — individual product reports (e.g. `/icbc/25G2488A/report.html`)
- `/<bank>/<code>/chart.svg` — net-value charts
- `/<bank>/<code>/meta.json` — structured product metadata
- `/<bank>/<code>/pdfs/*.pdf` — official disclosure PDFs

Hidden from web (server-side raw data only):
- `data.json` / `nv_full.json` / `reports.json` — raw API dumps
- `*.py` — build scripts
- `share_url.txt` — back-pointers to share-hosting

## Data source

Read-only mount of `/home/jingtao/finance/wealth-products/`. The dashboard regenerates on every `build_dashboard.py` run; nginx serves the freshly-written file with no restart needed.

## Deploy

```bash
cd ~/ai-learn/projects/licai
docker compose up -d
```

First request after fresh deploy may take 5-30s while letsencrypt-companion fetches a cert. Watch with:
```bash
docker logs -f nginx-proxy-acme | grep licai
```

## Related

- **share-hosting** (`share.ai.jingtao.fun`): UUID-only file share for ad-hoc reports
- **licai** (this): Dedicated dashboard + per-product browser for the wealth-products archive
