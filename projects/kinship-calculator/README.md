# Kinship Calculator (亲戚称呼计算器)

Chinese kinship term calculator - a single-page web app for quickly determining the correct Chinese kinship term for complex family relationships.

## How It Works

Click through a chain of relationships (e.g., 妈妈 → 爸爸 → 儿子) and instantly see the correct Chinese kinship term. The engine uses chain normalization and reduction rules to simplify relationship chains into standard kinship terms.

## Features

- 10 basic relationship buttons: 爸爸, 妈妈, 哥哥, 弟弟, 姐姐, 妹妹, 儿子, 女儿, 老公, 老婆
- Supports chains up to 8 levels deep
- Covers 80+ Chinese kinship terms including:
  - Paternal/maternal grandparents and great-grandparents
  - Uncles, aunts, and their spouses (paternal and maternal)
  - Cousins (堂 and 表 distinctions)
  - Nephews, nieces, and in-laws
- Mobile-first design for use at family gatherings
- Clickable chain pills to truncate/edit the relationship path
- Undo and reset controls

## Tech Stack

- Static HTML + CSS + JavaScript (no framework, no build step)
- Deployed via nginx:alpine Docker image

## Development

Open `index.html` directly in a browser - no server needed for development.

## Deployment

```bash
docker compose up -d
```

Requires `S_DOMAIN` environment variable set on the host. The app will be available at `kinship.${S_DOMAIN}`.
