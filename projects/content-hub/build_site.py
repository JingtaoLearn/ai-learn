#!/usr/bin/env python3
"""Render the generic two-level content navigation site."""

from __future__ import annotations

import argparse
import html
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from registry_schema import parse_published_at, validate_category, validate_item

DEFAULT_ARCHIVE = Path.home() / "content-hub"


@dataclass
class RenderedSite:
    root_html: str
    root_index: dict
    category_html: dict[str, str]
    category_index: dict[str, dict]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def safe_url(value: str) -> str:
    from registry_schema import ValidationError, _https_url

    try:
        _https_url("URL", value)
    except ValidationError:
        return "#"
    return esc(value)


BASE_CSS = """
:root{--bg:#f3efe7;--paper:#fffdf9;--ink:#25211d;--muted:#716960;--line:#ddd2c5;--accent:#b65438;--accent-soft:#f4dfd7;--shadow:0 18px 48px rgba(67,52,37,.09)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;-webkit-text-size-adjust:100%;text-size-adjust:100%}a{color:inherit}button,input{font:inherit}.page{width:min(1180px,100%);margin:auto;padding:34px 24px 72px}.hero{position:relative;overflow:hidden;padding:52px clamp(24px,6vw,72px);background:var(--paper);border:1px solid var(--line);border-radius:28px;box-shadow:var(--shadow)}.eyebrow{color:var(--accent);font-size:.76rem;font-weight:800;letter-spacing:.15em;text-transform:uppercase}h1{margin:.35rem 0 .8rem;font-family:Georgia,"Noto Serif SC",serif;font-size:clamp(2.4rem,7vw,5.2rem);font-weight:600;line-height:1;letter-spacing:-.045em;text-wrap:balance}.lede{max-width:760px;margin:0;color:var(--muted);font-size:clamp(1rem,2vw,1.16rem)}.summary-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:30px}.summary-stat{padding:14px 16px;background:#f7f2eb;border:1px solid #e8ddd1;border-radius:14px}.summary-stat b{display:block;color:var(--accent);font-family:Georgia,"Noto Serif SC",serif;font-size:1.7rem}.summary-stat span{color:var(--muted);font-size:.76rem}.section-head{display:flex;justify-content:space-between;align-items:end;gap:16px;margin:30px 3px 16px}.section-head h2{margin:0;font-family:Georgia,"Noto Serif SC",serif;font-size:1.65rem}.section-head span{color:var(--muted);font-size:.82rem}.category-grid,.item-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.category-card,.item-card{min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:20px;box-shadow:0 9px 28px rgba(67,52,37,.055)}.category-card{position:relative;overflow:hidden;padding:26px;min-height:275px;display:flex;flex-direction:column}.category-card::after{content:attr(data-icon);position:absolute;right:-12px;bottom:-30px;font-size:9rem;opacity:.055;filter:grayscale(1)}.category-top,.item-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.category-icon{font-size:2rem}.pill{padding:5px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.68rem;font-weight:800;letter-spacing:.04em;text-transform:uppercase}.category-card h3,.item-card h2{margin:18px 0 4px;font-family:Georgia,"Noto Serif SC",serif;font-size:clamp(1.55rem,3vw,2rem);line-height:1.15;overflow-wrap:anywhere}.subtitle{margin:0;color:var(--accent);font-size:.82rem;font-weight:700}.description,.item-summary{color:#4c463f;line-height:1.68}.category-meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:auto;padding-top:18px;color:var(--muted);font-size:.78rem}.category-link,.primary-link{position:relative;z-index:1;min-height:44px;display:inline-flex;align-items:center;justify-content:center;margin-top:18px;padding:0 16px;border-radius:11px;background:var(--accent);color:#fff;text-decoration:none;font-weight:750}.breadcrumb{display:flex;gap:8px;align-items:center;margin:0 0 18px;color:var(--muted);font-size:.84rem}.breadcrumb a{color:var(--accent);text-decoration:none}.controls{position:sticky;top:0;z-index:20;display:flex;gap:10px;margin:24px 0 16px;padding:10px;background:rgba(243,239,231,.94);border:1px solid var(--line);border-radius:16px;backdrop-filter:blur(12px)}.controls input{width:100%;min-height:46px;padding:0 14px;border:1px solid var(--line);border-radius:11px;background:var(--paper);outline:none}.controls input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(182,84,56,.12)}.item-card{display:flex;flex-direction:column;padding:24px}.item-path{margin:0;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.72rem;overflow-wrap:anywhere}.badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.item-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin-top:16px;padding:12px;background:#f7f3ed;border-radius:12px}.item-stat b{display:block;color:var(--ink);font-size:.95rem;overflow-wrap:anywhere}.item-stat span,.item-stat small{display:block;color:var(--muted);font-size:.69rem}.highlights{margin:15px 0 0;padding:13px 15px 13px 31px;background:#f6f1ea;border-radius:12px;font-size:.86rem}.item-meta{display:flex;flex-wrap:wrap;gap:7px 14px;margin-top:auto;padding-top:17px;color:var(--muted);font-size:.73rem}.actions{display:flex;gap:10px;margin-top:16px}.actions a{min-height:44px;display:inline-flex;align-items:center;justify-content:center;border-radius:11px;text-decoration:none;font-weight:750}.actions .primary-link{flex:1;margin:0}.source-link{padding:0 14px;border:1px solid var(--line);color:var(--accent)}.empty{display:none;padding:70px 20px;text-align:center;color:var(--muted);background:var(--paper);border:1px dashed var(--line);border-radius:20px}footer{margin-top:34px;color:var(--muted);text-align:center;font-size:.78rem}.accent-teal{--accent:#245e57;--accent-soft:#e3f0ed}.accent-blue{--accent:#1765a8;--accent-soft:#e2edf8}.accent-purple{--accent:#6d52a8;--accent-soft:#ece7f6}.accent-amber{--accent:#9a6424;--accent-soft:#f6ead6}.accent-green{--accent:#347354;--accent-soft:#e3f0e8}
@media(max-width:760px){.page{padding:18px 12px 56px}.hero{padding:34px 24px;border-radius:20px}.category-grid,.item-grid{grid-template-columns:1fr}.summary-stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:480px){.page{padding:8px 8px 42px}.hero{padding:28px 19px;border-radius:16px}h1{font-size:2.4rem}.summary-stats{grid-template-columns:1fr 1fr;gap:8px}.category-card,.item-card{padding:19px;border-radius:16px}.actions{flex-direction:column}.actions a{width:100%}}@media(max-width:360px){.summary-stats{grid-template-columns:1fr}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
"""


def _accent_class(accent: str) -> str:
    return "" if accent == "terracotta" else f" accent-{esc(accent)}"


def _category_card(category: dict, item_count: int, latest: str) -> str:
    category_id = esc(category["category_id"])
    return f"""<article class="category-card{_accent_class(category['accent'])}" data-icon="{esc(category['icon'])}">
<div class="category-top"><span class="category-icon">{esc(category['icon'])}</span><span class="pill">{item_count} {esc(category['item_label'])}</span></div>
<h3>{esc(category['title'])}</h3><p class="subtitle">{esc(category['subtitle'])}</p><p class="description">{esc(category['description'])}</p>
<div class="category-meta"><span>最近更新 {esc(latest or '—')}</span><span>来源 {esc(category['source_skill'])}</span></div>
<a class="category-link" href="/categories/{category_id}/">进入分类 <span aria-hidden="true">→</span></a></article>"""


def _item_card(item: dict, accent: str) -> str:
    badges = "".join(f'<span class="pill">{esc(value)}</span>' for value in item["badges"])
    stats = "".join(
        f'<div class="item-stat"><span>{esc(stat["label"])}</span><b>{esc(stat["value"])}</b><small>{esc(stat["sub"])}</small></div>'
        for stat in item["stats"]
    )
    highlights = "".join(f"<li>{esc(value)}</li>" for value in item["highlights"])
    tags = " · ".join(esc(tag) for tag in item["tags"])
    search_text = " ".join(
        [item["title"], item["subtitle"], item["summary"], *item["highlights"], *item["tags"]]
    ).lower()
    source_action = (
        f'<a class="source-link" href="{safe_url(item["source_url"])}" target="_blank" rel="noopener">来源</a>'
        if item["source_url"]
        else ""
    )
    return f"""<article class="item-card{_accent_class(accent)}" data-search="{esc(search_text)}">
<div class="item-top"><span class="pill">{esc(item['published_at'][:10])}</span><div class="badges">{badges}</div></div>
<h2>{esc(item['title'])}</h2><p class="item-path">{esc(item['subtitle'])}</p><p class="item-summary">{esc(item['summary'])}</p>
{f'<div class="item-stats">{stats}</div>' if stats else ''}{f'<ul class="highlights">{highlights}</ul>' if highlights else ''}
<div class="item-meta"><span>{tags or '未标注标签'}</span><span>{esc(item['source_skill'])}</span></div>
<div class="actions"><a class="primary-link" href="{safe_url(item['primary_url'])}" target="_blank" rel="noopener">打开内容 <span aria-hidden="true">→</span></a>{source_action}</div></article>"""


def _document(title: str, body: str, accent: str = "terracotta") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover"><meta name="description" content="Jingtao 的通用内容导航与注册中心"><title>{esc(title)}</title><style>{BASE_CSS}</style></head><body class="{_accent_class(accent).strip()}"><main class="page">{body}</main></body></html>"""


def build_site(categories: list[dict], items: list[dict]) -> RenderedSite:
    categories = [validate_category(card) for card in categories]
    items = [validate_item(card) for card in items]
    by_id = {card["category_id"]: card for card in categories}
    if len(by_id) != len(categories):
        raise ValueError("duplicate category_id")
    item_identities = [(item["category_id"], item["item_id"]) for item in items]
    if len(set(item_identities)) != len(item_identities):
        raise ValueError("duplicate item identity")
    for item in items:
        if item["category_id"] not in by_id:
            raise ValueError(f"orphan item category: {item['category_id']}")
    ordered_categories = sorted(categories, key=lambda card: (card["sort_order"], card["title"].lower()))
    items_by_category = {category_id: [] for category_id in by_id}
    for item in items:
        items_by_category[item["category_id"]].append(item)
    for category_items in items_by_category.values():
        category_items.sort(key=lambda card: parse_published_at(card["published_at"]), reverse=True)

    category_cards = []
    category_summaries = []
    category_html: dict[str, str] = {}
    category_index: dict[str, dict] = {}
    for category in ordered_categories:
        category_id = category["category_id"]
        category_items = items_by_category[category_id]
        latest = category_items[0]["published_at"][:10] if category_items else ""
        category_cards.append(_category_card(category, len(category_items), latest))
        summary = dict(category)
        summary.update(
            {
                "item_count": len(category_items),
                "latest_at": latest or None,
                "category_url": f"https://hub.ai.jingtao.fun/categories/{category_id}/",
            }
        )
        category_summaries.append(summary)
        cards_markup = "\n".join(_item_card(item, category["accent"]) for item in category_items)
        body = f"""<nav class="breadcrumb"><a href="/">全部分类</a><span>/</span><span>{esc(category['title'])}</span></nav>
<header class="hero{_accent_class(category['accent'])}"><div class="eyebrow">{esc(category['icon'])} {esc(category['subtitle'])}</div><h1>{esc(category['title'])}</h1><p class="lede">{esc(category['description'])}</p><div class="summary-stats"><div class="summary-stat"><b>{len(category_items)}</b><span>{esc(category['item_label'])}</span></div><div class="summary-stat"><b>{esc(latest or '—')}</b><span>最近更新</span></div><div class="summary-stat"><b>{len({tag for item in category_items for tag in item['tags']})}</b><span>内容标签</span></div></div></header>
<section class="controls"><input id="search" type="search" placeholder="搜索标题、摘要、标签或关键内容…" autocomplete="off"></section><div class="section-head"><h2>分类内容</h2><span id="result-count">{len(category_items)} 项</span></div><section class="item-grid" id="item-grid">{cards_markup}</section><div class="empty" id="empty">没有匹配的内容。</div><footer>Category: {esc(category_id)} · Content Hub for Jingtao</footer>
<script>(()=>{{const cards=[...document.querySelectorAll('.item-card')],input=document.getElementById('search'),count=document.getElementById('result-count'),empty=document.getElementById('empty');function apply(){{const q=input.value.trim().toLowerCase();let visible=0;for(const card of cards){{card.hidden=!!q&&!card.dataset.search.includes(q);if(!card.hidden)visible++}}count.textContent=`${{visible}} 项`;empty.style.display=visible?'none':'block'}}input.addEventListener('input',apply)}})();</script>"""
        category_html[category_id] = _document(
            f"{category['title']} — Content Hub", body, category["accent"]
        )
        category_index[category_id] = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "category": summary,
            "item_count": len(category_items),
            "items": category_items,
        }

    root_body = f"""<header class="hero"><div class="eyebrow">Combined content registry / 通用内容注册中心</div><h1>Jingtao Content Hub</h1><p class="lede">统一注册不同类别的研究、学习与决策内容。先选择类别，再进入该类别的专属导航页查看全部条目。</p><div class="summary-stats"><div class="summary-stat"><b>{len(ordered_categories)}</b><span>内容类别</span></div><div class="summary-stat"><b>{len(items)}</b><span>已注册条目</span></div><div class="summary-stat"><b>{sum(bool(values) for values in items_by_category.values())}</b><span>已有内容的类别</span></div></div></header><div class="section-head"><h2>选择一个类别</h2><span>两级导航 · 持续扩展</span></div><section class="category-grid">{''.join(category_cards)}</section><footer>hub.ai.jingtao.fun · Zhang for Jingtao</footer>"""
    root_index = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category_count": len(ordered_categories),
        "item_count": len(items),
        "categories": category_summaries,
    }
    return RenderedSite(
        root_html=_document("Jingtao Content Hub", root_body),
        root_index=root_index,
        category_html=category_html,
        category_index=category_index,
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_public_site(root: Path, categories: list[dict], items: list[dict]) -> None:
    root = Path(root).expanduser().resolve()
    candidate_archives = [DEFAULT_ARCHIVE]
    for parent in (root, *root.parents):
        if parent.name == ".releases":
            candidate_archives.append(parent.parent)
            break
    for archive in candidate_archives:
        current = archive / "current"
        if current.exists():
            active_release = current.resolve()
            if root == active_release or active_release in root.parents:
                raise RuntimeError(
                    "refusing to mutate the active public release; use register.py"
                )
    rendered = build_site(categories, items)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / "dashboard.html", rendered.root_html)
    _atomic_write(root / "index.json", json.dumps(rendered.root_index, ensure_ascii=False, indent=2) + "\n")
    categories_by_id = {card["category_id"]: card for card in categories}
    for category_id, category in categories_by_id.items():
        category_root = root / "categories" / category_id
        _atomic_write(category_root / "category.json", json.dumps(category, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(category_root / "index.html", rendered.category_html[category_id])
        _atomic_write(category_root / "index.json", json.dumps(rendered.category_index[category_id], ensure_ascii=False, indent=2) + "\n")
    for item in items:
        item_path = root / "categories" / item["category_id"] / "items" / item["item_id"] / "card.json"
        _atomic_write(item_path, json.dumps(item, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--categories", required=True, type=Path)
    parser.add_argument("--items", required=True, type=Path)
    args = parser.parse_args()
    categories = json.loads(args.categories.read_text(encoding="utf-8"))
    items = json.loads(args.items.read_text(encoding="utf-8"))
    write_public_site(args.root, categories, items)


if __name__ == "__main__":
    main()
