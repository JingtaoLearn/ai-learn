# Content Hub

A generic two-level navigation and registration platform for Jingtao's research, learning, and decision artifacts.

**Production URL:** https://hub.ai.jingtao.fun

## Information architecture

1. **Level 1 — category directory:** `/` lists every registered category with its item count and latest update.
2. **Level 2 — category dashboard:** `/categories/<category-id>/` lists all registered items in that category using a consistent searchable card layout.
3. **Primary artifacts:** item cards link to the canonical report, tool, or document URL. The hub stores public navigation metadata, not report bodies.

Examples of categories:

- `investment-research` — wealth, stock, fund, insurance, and other financial research
- `skill-learning` — bilingual Skill Learning reports
- future categories can be added without changing the renderer or Nginx configuration

## Registry API

One CLI owns both registration levels:

```bash
# Register or update a category
python3 ~/ai-learn/projects/content-hub/register.py category \
  --card-json /path/to/category.json

# Register or update an item inside an existing category
python3 ~/ai-learn/projects/content-hub/register.py item \
  --card-json /path/to/item.json
```

Category registration is idempotent by `category_id`. Item registration is idempotent by `(category_id, item_id)`. An item cannot be registered before its category exists.

## Atomic publication model

```text
~/content-hub/
├── current -> .releases/<release-id>
├── .registry.lock
└── .releases/<release-id>/
    ├── _registry/                         # private source of truth; blocked by Nginx
    │   ├── categories/<category-id>.json
    │   └── items/<category-id>/<item-id>.json
    ├── dashboard.html                     # level-1 category directory
    ├── index.json
    └── categories/<category-id>/
        ├── category.json
        ├── index.html                     # level-2 category dashboard
        ├── index.json
        └── items/<item-id>/card.json
```

Every registration:

1. acquires one OS file lock;
2. loads and revalidates the current private registry;
3. writes complete category/item state into a new staging release;
4. renders all level-1 and level-2 pages;
5. atomically swaps the public `current` symlink.

Nginx readers therefore see either the complete old release or the complete new release. `build_site.py` refuses to mutate the active release directly.

## Public whitelist

Nginx serves only:

- `/`, `/dashboard.html`, `/index.json`
- `/categories/<category-id>/`
- `/categories/<category-id>/{index.html,index.json,category.json}`
- `/categories/<category-id>/items/<item-id>/card.json`

Private `_registry/`, `.releases/`, lock files, scripts, temporary files, and unknown paths return 404.

## Test

```bash
cd ~/ai-learn/projects/content-hub
python3 -m unittest -v tests/test_registry.py
python3 -m py_compile register.py build_site.py registry_schema.py
docker run --rm \
  -v "$PWD/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine nginx -t
```

## Deploy

```bash
mkdir -p ~/content-hub
cd ~/ai-learn/projects/content-hub
docker compose up -d
curl -fsSI https://hub.ai.jingtao.fun/
```
