"use strict";
const $ = (s, el = document) => el.querySelector(s);
const ACT = { BUY: "买入", SELL: "卖出", HOLD: "持有", WAIT: "观望" };
const UP = "#f0524f", DOWN = "#1fb57f", GOLD = "#e8b04b", DIM = "#8a8f98", LINE = "#262b34";
const OBJ = { sharpe: "夏普比率", calmar: "收益/回撤", cagr: "年化收益" };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", "x-quant": "1", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { location.href = "/auth/login"; throw new Error("login"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
const toast = (msg) => { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 2600); };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const px = (v) => v == null ? "—" : v >= 1000 ? v.toLocaleString("en", { maximumFractionDigits: 2 }) : v >= 100 ? v.toFixed(2) : v.toFixed(3);
const pct = (v, d = 1) => v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const sgn = (v, d = 3) => v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`;
const cls = (v) => v > 0 ? "up" : v < 0 ? "down" : "";

setInterval(() => { $("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) + " 北京"; }, 1000);

/* ---------- overview ---------- */
function gauge(sig, inst) {
  const R = Math.max(Math.abs(inst.buy), Math.abs(inst.sell), Math.abs(sig.slope)) * 1.35 || 1;
  const at = (v) => `${Math.min(100, Math.max(0, (v + R) / (2 * R) * 100))}%`;
  return `<div class="gauge" title="趋势斜率 %/日">
    <div class="gauge-track">
      <span class="gauge-mark sell" style="left:${at(inst.sell)}"></span>
      <span class="gauge-mark buy" style="left:${at(inst.buy)}"></span>
      <span class="gauge-needle" style="left:${at(sig.slope)}"></span>
    </div>
    <div class="gauge-labels"><span class="down">卖 ${sgn(inst.sell)}</span><span class="${cls(sig.slope)}">斜率 ${sgn(sig.slope)}%/日</span><span class="up">买 ${sgn(inst.buy)}</span></div>
  </div>`;
}

function trigger(sig, unit) {
  if (sig.buy_trigger) return `<div class="trigger"><span class="dim">收盘 ≥ <b class="up">${px(sig.buy_trigger)}</b> 触发买入</span><span class="dist up">${pct(sig.buy_trigger / sig.close - 1, 2)}</span></div>`;
  if (sig.sell_trigger) return `<div class="trigger"><span class="dim">收盘 ≤ <b class="down">${px(sig.sell_trigger)}</b> 触发卖出</span><span class="dist down">${pct(sig.sell_trigger / sig.close - 1, 2)}</span></div>`;
  return `<div class="trigger"><span class="faint">${sig.position ? "斜率已低于卖线，等待回升后再下穿" : "斜率已高于买线，等待回落后再上穿"}</span></div>`;
}

function spark(values, held) {
  if (!values || values.length < 2) return "";
  const w = 300, h = 54, lo = Math.min(...values), hi = Math.max(...values);
  const pts = values.map((v, i) => [i / (values.length - 1) * w, h - 4 - (v - lo) / (hi - lo || 1) * (h - 8)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const c = values.at(-1) >= values[0] ? UP : DOWN;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="g${held}${c.slice(1)}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".28"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d}L${w},${h}L0,${h}Z" fill="url(#g${held}${c.slice(1)})"/><path d="${d}" fill="none" stroke="${c}" stroke-width="1.6"/></svg>`;
}

/* the user's real position once they have confirmed anything; until then follow the model */
const myHeld = (item) => (item.me?.tracked ? item.me.held : !!item.signal?.position);

function card(item, i) {
  const inst = item.instrument;
  if (!item.ready) return `<article class="card" style="animation-delay:${i * 60}ms"><h2>${esc(inst.name)}</h2><p class="dim">${inst.refresh_error ? "数据获取失败：" + esc(inst.refresh_error) : "正在获取历史数据…"}</p></article>`;
  const s = item.signal, me = item.me;
  const mine = me.tracked ? (me.held ? '<span class="up">● 你持有</span>' : '<span class="dim">○ 你空仓</span>') : '<span class="faint">模型' + (s.position ? "持有" : "空仓") + "</span>";
  return `<article class="card ${myHeld(item) ? "held" : ""} act-${s.action}" style="animation-delay:${i * 60}ms" data-href="#/s/${encodeURIComponent(inst.symbol)}">
    <div class="card-head">
      <div><h2>${esc(inst.name)}</h2><div class="code">${esc(inst.symbol)} · ${s.date}${s.provisional ? " · 临时收盘" : ""}</div></div>
      <span class="badge ${s.action}">${s.action === "BUY" || s.action === "SELL" ? "明日开盘" : "模型"}${ACT[s.action]}</span>
    </div>
    <div class="price-row"><span class="price">${px(s.close)}</span><span class="chg ${cls(s.change)}">${pct(s.change, 2)}</span>
      <span class="small" style="margin-left:auto">${mine}${me.held && me.pnl != null ? ` <span class="${cls(me.pnl)}">${pct(me.pnl)}</span>` : ""}</span></div>
    ${spark(item.spark, s.position)}
    ${gauge(s, inst)}
    ${trigger(s, inst.unit)}
  </article>`;
}

function feedRow(e, names) {
  const d = e.detail;
  let what;
  if (e.kind === "alert") what = `⚡ 盘中 ${e.action === "BUY" ? '<span class="up">突破买入价</span>' : '<span class="down">跌破卖出价</span>'} 现价 ${px(d.price)} / 触发 ${px(d.trigger)}`;
  else if (e.kind === "params") what = `参数更新 ${d.from.window}/${d.from.ema}/${d.from.buy}/${d.from.sell} → <span class="gold">${d.to.window}/${d.to.ema}/${d.to.buy}/${d.to.sell}</span>`;
  else what = `<span class="badge ${e.action}">${ACT[e.action]}</span> 收盘 ${px(d.close)}，斜率 ${sgn(d.slope)}%/日`;
  return `<div class="feed-row"><span class="mono faint small">${e.date.slice(0, 10)}</span><span>${esc(names[e.symbol] || e.symbol)}</span><span>${what}</span></div>`;
}

/* to-do items, written from the user's position; each carries its own buttons */
const tradeBtn = (i, side, date, price, label, signal_date, ghost) =>
  `<button class="sm${ghost ? " ghost" : ""}" data-trade='${esc(JSON.stringify({ sym: i.symbol, name: i.name, side, date, price, signal_date }))}'>${label}</button>`;

function todos(items) {
  const out = [];
  for (const it of items.filter((x) => x.ready)) {
    const { instrument: i, signal: s, me } = it;
    for (const p of me.pending) {
      const buy = p.action === "BUY", today = p.date === s.date;
      const ctx = buy ? (me.held ? "你已持有，可忽略" : "")
        : me.tracked && !me.held ? "你已空仓，可忽略" : me.pnl != null ? `你持有，浮盈 ${pct(me.pnl)}` : "";
      out.push({ tone: buy ? "up" : "down", sym: i.symbol,
        title: `${i.name}：${today ? "下一交易日开盘" : p.date.slice(5) + " 信号 · "}${buy ? "买入" : "卖出"}`,
        sub: (today ? `收盘斜率已穿越${buy ? "买线" : "卖线"}，执行后点确认` : "这个信号你执行了吗？") + (ctx ? " · " + ctx : ""),
        btns: tradeBtn(i, p.action, p.fill?.date, p.fill?.price, buy ? "我已买入" : "我已卖出", p.date) +
          `<button class="sm ghost" data-skip='${esc(JSON.stringify({ sym: i.symbol, signal_date: p.date, action: p.action }))}'>${buy ? "没买" : "没卖"}</button>` });
    }
    if (me.divergence === "MODEL_FLAT")
      out.push({ tone: "warn", sym: i.symbol, title: `${i.name}：模型已离场，你仍持有`,
        sub: `浮盈 ${pct(me.pnl)} · 模型空仓期间，下跌风险由你承担`, btns: tradeBtn(i, "SELL", s.date, s.close, "登记卖出") });
    if (me.divergence === "MODEL_LONG")
      out.push({ tone: "warn", sym: i.symbol, title: `${i.name}：模型持有中，你空仓`,
        sub: "可以等下一个买点，或自行买入后登记", btns: tradeBtn(i, "BUY", s.date, s.close, "登记买入", null, true) });
  }
  return out;
}

function holdingsTable(items) {
  const rows = items.filter((x) => x.ready && myHeld(x));
  if (!rows.length) return '<div class="empty">当前没有持仓</div>';
  return `<table class="metrics hold"><thead><tr><th>标的</th><th>现价</th><th>成本</th><th>浮盈</th><th>持有</th><th>模型</th><th>卖出触发</th></tr></thead><tbody>
    ${rows.map(({ instrument: i, signal: s, me }) => {
      const days = me.entry_date ? Math.round((Date.parse(s.date) - Date.parse(me.entry_date)) / 864e5) : null;
      const trig = s.sell_trigger;
      return `<tr data-href="#/s/${encodeURIComponent(i.symbol)}"><td>${esc(i.name)}${me.tracked ? "" : ' <span class="chip">按模型</span>'}</td>
        <td>${px(s.close)}</td><td>${me.entry_price ? px(me.entry_price) : "—"}</td>
        <td class="${cls(me.pnl)}">${me.pnl != null ? pct(me.pnl) : "—"}</td><td>${days != null ? days + " 天" : "—"}</td>
        <td>${s.position ? '<span class="up">持有</span>' : '<span class="down">已离场</span>'}</td>
        <td>${trig ? `${px(trig)} <span class="faint">${pct(trig / s.close - 1)}</span>` : "—"}</td></tr>`;
    }).join("")}</tbody></table>`;
}

function myTrades(items) {
  const done = items.filter((x) => x.ready).flatMap((x) => x.me.realized.map((t) => ({ ...t, name: x.instrument.name })))
    .sort((a, b) => b.exit_date.localeCompare(a.exit_date));
  if (!done.length) return '<div class="empty">还没有完成的交易。按信号买卖后点「我已买入 / 我已卖出」，这里会记录你的真实收益。</div>';
  const wins = done.filter((t) => t.return > 0).length, total = done.reduce((a, t) => a * (1 + t.return), 1) - 1;
  return `<p class="note">共 <b>${done.length}</b> 笔 · 胜率 <b>${pct(wins / done.length, 0)}</b> · 复利合计 <b class="${cls(total)}">${pct(total)}</b></p>
    <table class="metrics"><thead><tr><th>标的</th><th>买入</th><th>卖出</th><th>收益</th></tr></thead><tbody>
    ${done.map((t) => `<tr><td>${esc(t.name)}</td><td>${t.entry_date} @ ${px(t.entry)}</td><td>${t.exit_date} @ ${px(t.exit)}</td><td class="${cls(t.return)}">${pct(t.return)}</td></tr>`).join("")}</tbody></table>`;
}

async function overview() {
  const view = $("#view");
  const { items, events } = await api("/api/overview");
  const ready = items.filter((x) => x.ready);
  const names = Object.fromEntries(items.map((x) => [x.instrument.symbol, x.instrument.name]));
  const latest = ready.map((x) => x.signal.date).sort().at(-1) || "";
  const list = todos(items);
  const tracked = ready.some((x) => x.me.tracked);
  const held = ready.filter(myHeld);
  view.innerHTML = `
    <section class="verdict">
      <div><div class="eyebrow">${latest} 收盘 · ${tracked ? "按你的真实持仓" : "按模型仓位"}</div>
        <h1>${list.length ? `你有 <em>${list.length}</em> 件事要处理` : "今天不用操作"}</h1>
        <p>${list.length ? "执行了就点确认、没执行就点「没买 / 没卖」，之后的提醒都按你的真实仓位来。"
          : `持有 ${held.length} 个 · 观察 ${ready.length - held.length} 个。离触发价越近，下个收盘越可能出信号。`}</p></div>
    </section>
    ${list.length ? `<section class="todo">${list.map((t, k) => `<div class="todo-item ${t.tone}" style="animation-delay:${k * 70}ms">
      <div class="todo-text" data-href="#/s/${encodeURIComponent(t.sym)}"><b>${esc(t.title)}</b><div class="small dim">${esc(t.sub)}</div></div>
      <div class="todo-btns">${t.btns}</div></div>`).join("")}</section>` : ""}
    ${tracked ? "" : `<p class="note onboard">💡 还没登记你的真实持仓，下面暂按模型仓位展示。进入标的点「登记交易」，或在信号出现后点确认，之后所有提醒都按你的实际仓位。</p>`}
    <div class="section-title"><h3>我的持仓</h3><span class="small faint">点行进入详情</span></div>
    <section class="panel">${holdingsTable(items)}</section>
    <div class="section-title"><h3>观察列表</h3><span class="small faint">卡片底部是距触发价的涨跌幅</span></div>
    <section class="grid">${items.map(card).join("")}</section>
    <div class="section-title"><h3>我的交易</h3><span class="small faint">你确认过的真实买卖</span></div>
    <section class="panel">${myTrades(items)}</section>
    <div class="section-title"><h3>信号日志</h3><span class="small faint">收盘信号 · 盘中预警 · 参数变更</span></div>
    <section class="feed">${events.length ? events.map((e) => feedRow(e, names)).join("") : '<div class="empty">暂无记录，首次收盘任务运行后出现</div>'}</section>`;
  if (items.some((x) => !x.ready && !x.instrument.refresh_error)) setTimeout(() => route(), 4000);
}

/* ---------- record a real trade / skip a signal ---------- */
function tradeDialog(o) {
  const d = $("#dlg-trade");
  d.dataset.sym = o.sym; d.dataset.signal = o.signal_date || "";
  $("#t-title").textContent = `${o.name} · ${o.side === "BUY" ? "登记买入" : "登记卖出"}`;
  $("#t-side").value = o.side;
  $("#t-date").value = o.date || new Date().toISOString().slice(0, 10);
  $("#t-price").value = o.price ? +(+o.price).toFixed(4) : "";
  $("#t-qty").value = ""; $("#t-note").value = "";
  $("#t-hint").textContent = o.signal_date ? `对应 ${o.signal_date} 收盘信号。预填的是模型假设的次日开盘价，请改成你的实际成交价。` : "填你的实际成交日期和价格。";
  d.showModal();
}
async function saveTrade() {
  const d = $("#dlg-trade");
  const body = { side: $("#t-side").value, date: $("#t-date").value, price: +$("#t-price").value,
    qty: $("#t-qty").value ? +$("#t-qty").value : null, note: $("#t-note").value || null, signal_date: d.dataset.signal || null };
  if (!(body.price > 0)) return toast("请填写成交价");
  try { await api(`/api/instruments/${encodeURIComponent(d.dataset.sym)}/trades`, { method: "POST", body }); d.close(); toast("已记录"); route(); }
  catch (e) { toast(e.message); }
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-trade],[data-skip],[data-del-trade],[data-href]");
  if (!t) return;
  e.stopPropagation();
  if (t.dataset.trade) return tradeDialog(JSON.parse(t.dataset.trade));
  if (t.dataset.skip) {
    const s = JSON.parse(t.dataset.skip);
    await api(`/api/instruments/${encodeURIComponent(s.sym)}/skip`, { method: "POST", body: { signal_date: s.signal_date, action: s.action } });
    toast(s.action === "BUY" ? "已记为没买，按空仓继续提醒" : "已记为没卖，按持有继续提醒"); return route();
  }
  if (t.dataset.delTrade) {
    if (!confirm("删除这条交易记录？")) return;
    await api(`/api/trades/${t.dataset.delTrade}`, { method: "DELETE" }); toast("已删除"); return route();
  }
  location.hash = t.dataset.href;
});

/* ---------- detail ---------- */
const chartBase = (el, h) => LightweightCharts.createChart(el, {
  height: h, autoSize: true,
  layout: { background: { color: "transparent" }, textColor: DIM, fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
  grid: { vertLines: { color: "rgba(38,43,52,.5)" }, horzLines: { color: "rgba(38,43,52,.5)" } },
  rightPriceScale: { borderColor: LINE }, timeScale: { borderColor: LINE },
  crosshair: { mode: 0 }, localization: { locale: "zh-CN" },
});

let D = null; // current detail state

function markers(dates, pos) {
  const out = [];
  for (let t = 1; t < pos.length; t++) {
    if (pos[t] === pos[t - 1]) continue;
    const buy = pos[t] === 1, pending = t === pos.length - 1;
    out.push({ time: pending ? dates[t] : dates[t + 1], position: buy ? "belowBar" : "aboveBar", color: buy ? UP : DOWN,
      shape: pending ? "circle" : buy ? "arrowUp" : "arrowDown", text: pending ? `明日${buy ? "买" : "卖"}` : buy ? "买" : "卖" });
  }
  return out;
}

function drawCharts(v, p) {
  const dates = v.bars.map((b) => b[0]);
  const s = D.series;
  s.candle.setData(v.bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] })));
  s.trend.setData(dates.map((t, i) => v.trend[i] == null ? { time: t } : { time: t, value: v.trend[i] }));
  s.hold.setData(dates.map((t, i) => ({ time: t, value: i >= v.start_index && v.position[i - 1] ? 1 : 0 })));
  const known = new Set(dates);
  const mine = (D.v.me ? D.v.me.trades : []).filter((t) => known.has(t.date)).map((t) => ({ time: t.date, position: t.side === "BUY" ? "belowBar" : "aboveBar",
    color: GOLD, shape: "square", text: t.side === "BUY" ? "我买" : "我卖" }));
  s.candle.setMarkers([...markers(dates, v.position).filter((m) => m.time >= dates[v.start_index]), ...mine]
    .sort((a, b) => a.time.localeCompare(b.time)));
  s.slope.setData(dates.map((t, i) => v.slope[i] == null ? { time: t } : { time: t, value: v.slope[i], color: v.slope[i] >= 0 ? "rgba(240,82,79,.6)" : "rgba(31,181,127,.6)" }));
  (D.lines || []).forEach((l) => s.slope.removePriceLine(l));
  D.lines = [
    s.slope.createPriceLine({ price: p.buy, color: UP, lineWidth: 1, lineStyle: 2, title: "买线" }),
    s.slope.createPriceLine({ price: p.sell, color: DOWN, lineWidth: 1, lineStyle: 2, title: "卖线" }),
  ];
  const bt = v.backtest;
  s.eq.setData(bt.dates.map((t, i) => ({ time: t, value: bt.equity[i] })));
  s.bh.setData(bt.dates.map((t, i) => ({ time: t, value: bt.buy_hold_equity[i] })));
  D.eq.timeScale().fitContent();
}

function metricsTable(rows) {
  const keys = [["total_return", "累计收益", pct], ["cagr", "年化收益", pct], ["max_drawdown", "最大回撤", pct], ["sharpe", "夏普比率", (v) => v.toFixed(2)], ["volatility", "年化波动", pct]];
  return `<table class="metrics"><thead><tr><th></th>${rows.map((r) => `<th>${r[0]}</th>`).join("")}</tr></thead><tbody>
    ${keys.map(([k, label, f]) => {
      const vals = rows.map((r) => r[1]?.[k]);
      const best = k === "volatility" ? null : Math.max(...vals.filter((x) => x != null));
      return `<tr><td>${label}</td>${vals.map((x) => `<td class="${x === best && rows.length > 1 ? "gold" : ""}">${x == null ? "—" : f(x)}</td>`).join("")}</tr>`;
    }).join("")}</tbody></table>`;
}

/* inline diverging bar: red up / green down, scaled to the column's largest move */
const bar = (v, max) => v == null ? "—" : `<span class="bar"><i class="${v >= 0 ? "pos" : "neg"}" style="width:${Math.min(100, Math.abs(v) / (max || 1) * 100)}%"></i></span><span class="${cls(v)}">${pct(v)}</span>`;

function periodsTable(rows) {
  if (!rows?.length) return "";
  const max = Math.max(...rows.flatMap((r) => [Math.abs(r.strategy), Math.abs(r.buy_hold)]));
  const beat = rows.filter((r) => r.strategy > r.buy_hold).length;
  const safer = rows.filter((r) => r.strategy_mdd > r.buy_hold_mdd).length;
  return `<h4 style="margin-top:6px">分年度表现</h4>
    <p class="note">${rows.length} 个年度中，策略收益跑赢持有 <b>${beat}</b> 年，回撤小于持有 <b>${safer}</b> 年。首尾年份不是完整年度。</p>
    <div class="xscroll"><table class="metrics periods"><thead><tr><th>年份</th><th>策略</th><th>买入持有</th><th>超额</th><th>策略回撤</th><th>持有回撤</th><th>交易</th><th>持仓</th></tr></thead><tbody>
    ${rows.slice().reverse().map((r) => `<tr><td>${r.period}</td><td>${bar(r.strategy, max)}</td><td>${bar(r.buy_hold, max)}</td>
      <td class="${cls(r.strategy - r.buy_hold)}">${pct(r.strategy - r.buy_hold)}</td>
      <td class="${r.strategy_mdd > r.buy_hold_mdd ? "gold" : ""}">${pct(r.strategy_mdd)}</td><td>${pct(r.buy_hold_mdd)}</td>
      <td>${r.trades}</td><td>${(r.exposure * 100).toFixed(0)}%</td></tr>`).join("")}</tbody></table></div>`;
}

function renderBacktest(v) {
  const st = v.backtest.strategy;
  $("#bt").innerHTML = metricsTable([["趋势策略", st], ["买入持有", v.backtest.buy_hold]]) +
    `<p class="note">自 <b>${v.backtest.dates[0]}</b> 起回测 · 完成 <b>${st.trades}</b> 笔交易 · 胜率 <b>${st.win_rate == null ? "—" : pct(st.win_rate, 0)}</b> · 持仓时间占比 <b>${pct(st.exposure, 0)}</b>。收盘出信号、次日开盘成交，已扣除买卖成本。</p>`;
  $("#periods").innerHTML = periodsTable(v.backtest.periods);
  const tr = v.backtest.trades.slice().reverse();
  $("#trades").innerHTML = tr.length ? `<table class="metrics"><thead><tr><th>买入</th><th>价格</th><th>卖出</th><th>价格</th><th>收益</th></tr></thead><tbody>
    ${tr.map((t) => `<tr><td>${t.entry_date}</td><td>${px(t.entry)}</td><td>${t.open ? '<span class="gold">持有中</span>' : t.exit_date}</td><td>${px(t.exit)}</td><td class="${cls(t.return)}">${pct(t.return)}</td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">回测期内无交易</div>';
}

function paramValues() {
  return { window: +$("#p-window").value, ema: +$("#p-ema").value, buy: +$("#p-buy").value, sell: +$("#p-sell").value, start_date: $("#p-start").value };
}

function setParams(p) {
  for (const k of ["window", "ema", "buy", "sell"]) $(`#p-${k}`).value = p[k];
  if (p.start_date) $("#p-start").value = p.start_date;
  onParamInput();
}

function onParamInput() {
  const p = paramValues(), i = D.inst;
  const dirty = p.window !== i.window || p.ema !== i.ema || p.buy !== i.buy || p.sell !== i.sell || p.start_date !== i.start_date;
  $("#p-state").className = "chip" + (dirty ? " dirty" : "");
  $("#p-state").textContent = dirty ? "预览中 · 未保存" : "当前生效参数";
  $("#p-save").disabled = !dirty;
  clearTimeout(D.deb);
  D.deb = setTimeout(async () => {
    if (!(p.window >= 2 && p.ema >= 1 && p.buy > p.sell)) return;
    try {
      const v = await api(`/api/instruments/${D.sym}/preview`, { method: "POST", body: p });
      drawCharts({ ...D.v, ...v }, p);
      renderBacktest(v);
    } catch (e) { toast(e.message); }
  }, 300);
}

async function saveParams() {
  const p = paramValues();
  await api(`/api/instruments/${D.sym}/apply`, { method: "POST", body: p });
  if (p.start_date !== D.inst.start_date) await api(`/api/instruments/${D.sym}`, { method: "PATCH", body: { start_date: p.start_date } });
  toast("参数已保存，之后的信号按新参数计算");
  route();
}

function heatmap(h) {
  const vals = h.score.flat().filter((x) => x != null), lo = Math.min(...vals), hi = Math.max(...vals);
  const color = (v) => v == null ? "transparent" : `rgba(232,176,75,${0.08 + 0.85 * (v - lo) / (hi - lo || 1)})`;
  return `<div class="heat" style="grid-template-columns:44px repeat(${h.ema.length},1fr)">
    <div class="hd">窗口\\EMA</div>${h.ema.map((e) => `<div class="hd">${e}</div>`).join("")}
    ${h.window.map((w, i) => `<div class="hd">${w}</div>${h.score[i].map((v) => `<div style="background:${color(v)}" title="${v}">${v == null ? "" : v.toFixed(2)}</div>`).join("")}`).join("")}</div>`;
}

function stagesTable(folds) {
  const max = Math.max(...folds.flatMap((f) => [f.strategy, f.current, f.buy_hold].filter(Boolean).map((m) => Math.abs(m.total_return))));
  const best = (f) => { const c = [["s", f.strategy.total_return], ["c", f.current?.total_return], ["b", f.buy_hold.total_return]].filter((x) => x[1] != null); return c.sort((a, b) => b[1] - a[1])[0][0]; };
  return `<div class="xscroll"><table class="metrics periods"><thead><tr><th>阶段</th><th>检验区间</th><th>所选参数</th><th>滚动优化</th><th>当前参数</th><th>买入持有</th><th>优化回撤</th><th>持有回撤</th><th>买卖次数</th></tr></thead><tbody>
    ${folds.map((f, k) => { const w = best(f); return `<tr><td>${k + 1}</td><td>${f.test_start}<br><span class="faint">~ ${f.test_end}</span></td>
      <td>${f.params.window}/${f.params.ema}/${f.params.buy}/${f.params.sell}</td>
      <td class="${w === "s" ? "win" : ""}">${bar(f.strategy.total_return, max)}</td>
      <td class="${w === "c" ? "win" : ""}">${f.current ? bar(f.current.total_return, max) : "—"}</td>
      <td class="${w === "b" ? "win" : ""}">${bar(f.buy_hold.total_return, max)}</td>
      <td>${pct(f.strategy.max_drawdown)}</td><td>${pct(f.buy_hold.max_drawdown)}</td><td>${f.trades ?? "—"}</td></tr>`; }).join("")}
    </tbody></table></div><p class="note">金色底＝该阶段收益最高的一方。所选参数格式：窗口/EMA/买线/卖线。</p>`;
}

function drawWalkForward(wf) {
  const el = $("#c-wf");
  if (!el || !wf.equity) return;
  const ch = chartBase(el, 240);
  const line = (vals, opts) => { const s = ch.addLineSeries({ priceLineVisible: false, lastValueVisible: true, ...opts }); s.setData(wf.dates.map((t, i) => ({ time: t, value: vals[i] }))); return s; };
  const strat = line(wf.equity, { color: GOLD, lineWidth: 2, title: "优化" });
  if (wf.current_equity) line(wf.current_equity, { color: "#e9e6df", lineWidth: 1, lineStyle: 2, title: "当前" });
  line(wf.buy_hold_equity, { color: DIM, lineWidth: 1, title: "持有" });
  const known = new Set(wf.dates);
  strat.setMarkers(wf.folds.filter((f) => known.has(f.test_start)).map((f, k) => ({ time: f.test_start, position: "aboveBar", color: GOLD, shape: "arrowDown", text: `阶段${k + 1}` })));
  ch.timeScale().fitContent();
}

function renderOpt(runs) {
  const el = $("#opt");
  const run = runs[0];
  if (!run) { el.innerHTML = `<p class="note">在训练机（Feng，8 核）上做网格搜索：窗口 × EMA × 买线 × 卖线共约 9,500 组参数。评分先在相邻参数之间做平均，选的是<b>一片都表现好的区域</b>，而不是某个碰巧最好的单点。再用<b>滚动前推</b>验证：每一段都只用之前的数据选参数，在之后的数据上检验，看这个方法在样本外是否真的有效。</p>`; return; }
  if (run.status === "queued" || run.status === "running") {
    el.innerHTML = `<p><span class="spinner"></span> 正在${run.runner === "feng" ? "训练机 Feng" : "本机"}上优化（${OBJ[run.objective]}）…</p>`;
    clearTimeout(D.poll); D.poll = setTimeout(async () => { const v = await api(`/api/instruments/${D.sym}`); if (D?.sym === v.instrument.symbol) renderOpt(v.optimizations); }, 2500);
    return;
  }
  if (run.status === "failed") { el.innerHTML = `<p class="down">优化失败：${esc(run.error)}</p>`; return; }
  const r = run.result, wf = r.walk_forward, rec = r.recommended;
  const verdict = wf.strategy.sharpe > wf.buy_hold.sharpe
    ? "✅ 滚动优化在样本外的风险调整收益（夏普比率）<b>高于</b>买入持有。"
    : wf.strategy.max_drawdown > wf.buy_hold.max_drawdown
      ? "➖ 滚动优化在样本外的夏普比率没有超过买入持有，但<b>回撤更小</b>，它的价值主要在控制风险。"
      : "⚠️ 滚动优化在样本外<b>不如</b>买入持有，这个标的上择时的优势没有被验证，信号只宜作参考。";
  el.innerHTML = `
    <div class="rec"><span>推荐参数</span><b>窗口 ${rec.window} · EMA ${rec.ema} · 买 ${sgn(rec.buy)} · 卖 ${sgn(rec.sell)}</b>
      <button class="sm ghost" id="opt-try">载入预览</button><button class="sm" id="opt-apply">采用</button>
      <span class="small faint" style="margin-left:auto">${run.runner === "feng" ? "训练机 Feng" : "本机"} · ${r.combos} 组 · ${r.seconds}s · ${OBJ[r.objective]}</span></div>
    <h4>样本外检验（滚动前推，${wf.dates[0]} 起）</h4>
    ${metricsTable([["滚动优化", wf.strategy], ["当前参数", wf.current], ["买入持有", wf.buy_hold]])}
    <p class="note">${verdict}「当前参数」通常是用全部历史数据挑出来的，含事后偏差，样本外表现可能被高估。</p>
    <h4 style="margin-top:14px">样本外净值（金＝滚动优化　白虚线＝当前参数　灰＝买入持有　竖标＝阶段起点）</h4>
    <div id="c-wf" class="chart eq"></div>
    <h4 style="margin-top:14px">各阶段表现（每段只用之前的数据选参数）</h4>
    ${stagesTable(wf.folds)}
    <h4 style="margin-top:14px">参数稳定性（窗口 × EMA）</h4>${heatmap(r.heatmap)}`;
  drawWalkForward(wf);
  $("#opt-try").onclick = () => { setParams(rec); window.scrollTo({ top: 0, behavior: "smooth" }); toast("已载入推荐参数，图表为预览效果"); };
  $("#opt-apply").onclick = async () => { if (!confirm("采用推荐参数？之后的每日信号将按新参数计算。")) return; await api(`/api/instruments/${D.sym}/apply`, { method: "POST", body: rec }); toast("已采用"); route(); };
}

function myPanel(v) {
  const { instrument: i, signal: s, me } = v;
  const status = !me.tracked ? "还没有登记你在这个标的上的交易，提醒暂按模型仓位。"
    : me.held ? `你持有，${me.entry_date} 以 ${px(me.entry_price)} 买入，浮盈 <b class="${cls(me.pnl)}">${pct(me.pnl)}</b>${me.qty ? `，数量 ${me.qty}` : ""}。`
    : "你目前空仓。";
  const warn = (t, d) => `<div class="todo-item warn"><div><b>${t}</b><div class="small dim">${d}</div></div></div>`;
  const div = me.divergence === "MODEL_FLAT" ? warn("模型已离场，你仍持有", "模型空仓期间的下跌风险由你承担")
    : me.divergence === "MODEL_LONG" ? warn("模型持有中，你空仓", "可等下一个买点，或自行买入后登记") : "";
  const pend = me.pending.map((p) => `<div class="todo-item ${p.action === "BUY" ? "up" : "down"}"><div class="todo-text"><b>${p.date} ${p.action === "BUY" ? "买入" : "卖出"}信号</b><div class="small dim">执行了吗？</div></div>
    <div class="todo-btns">${tradeBtn(i, p.action, p.fill?.date, p.fill?.price, p.action === "BUY" ? "我已买入" : "我已卖出", p.date)}
    <button class="sm ghost" data-skip='${esc(JSON.stringify({ sym: i.symbol, signal_date: p.date, action: p.action }))}'>${p.action === "BUY" ? "没买" : "没卖"}</button></div></div>`).join("");
  const rows = me.trades.slice().reverse().map((t) => `<tr><td>${t.date}</td><td class="${t.side === "BUY" ? "up" : "down"}">${t.side === "BUY" ? "买入" : "卖出"}</td><td>${px(t.price)}</td><td>${t.qty ?? "—"}</td><td>${t.signal_date ? "按信号" : "自主"}</td><td>${esc(t.note || "")}</td><td><button class="sm ghost" data-del-trade="${t.id}">删除</button></td></tr>`).join("");
  return `<section class="panel"><h4><span>我的仓位与交易</span><span style="display:flex;gap:6px;text-transform:none;letter-spacing:0">
      ${tradeBtn(i, "BUY", s.date, s.close, "登记买入", null, true)}${tradeBtn(i, "SELL", s.date, s.close, "登记卖出", null, true)}</span></h4>
    <p class="note">${status}</p>${div}${pend}
    ${rows ? `<div class="tbl-wrap"><table class="metrics"><thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>来源</th><th>备注</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
  </section>`;
}

async function detail(sym) {
  const view = $("#view");
  view.innerHTML = `<p class="dim"><span class="spinner"></span> 加载中…</p>`;
  const v = await api(`/api/instruments/${encodeURIComponent(sym)}`);
  const inst = v.instrument;
  if (!v.ready) { view.innerHTML = `<a class="back" href="#/">← 总览</a><p>数据尚未就绪。</p>`; return; }
  const s = v.signal;
  D = { sym, inst, v };
  view.innerHTML = `
    <a class="back" href="#/">← 总览</a>
    <section class="d-head">
      <div><div class="code mono faint">${esc(inst.symbol)} · ${esc(inst.code)} · ${s.date}</div>
        <h1>${esc(inst.name)} <span class="badge ${s.action}" style="vertical-align:middle;font-size:14px">${ACT[s.action]}</span></h1>
        <div class="price-row"><span class="price">${px(s.close)}</span><span class="chg ${cls(s.change)}">${pct(s.change, 2)}</span><span class="small dim">${esc(inst.unit)}</span></div></div>
      <div class="d-stats">
        <div class="stat"><label>你的仓位</label><b class="${v.me.held ? "up" : ""}">${v.me.tracked ? (v.me.held ? "持有" : "空仓") : "未登记"}</b></div>
        ${v.me.held && v.me.pnl != null ? `<div class="stat"><label>浮盈</label><b class="${cls(v.me.pnl)}">${pct(v.me.pnl)}</b></div>` : ""}
        <div class="stat"><label>模型仓位</label><b class="${s.position ? "up" : ""}">${s.position ? "持有" : "空仓"}</b></div>
        <div class="stat"><label>趋势斜率</label><b class="${cls(s.slope)}">${sgn(s.slope)}%</b></div>
        <div class="stat"><label>${s.buy_trigger ? "买入触发" : "卖出触发"}</label><b class="${s.buy_trigger ? "up" : "down"}">${px(s.buy_trigger || s.sell_trigger)}</b></div>
        <div class="stat"><label>距触发</label><b>${s.buy_trigger || s.sell_trigger ? pct((s.buy_trigger || s.sell_trigger) / s.close - 1, 2) : "—"}</b></div>
      </div>
    </section>
    ${myPanel(v)}
    <section class="panel"><h4><span>价格 · 趋势线 · 买卖点</span><span class="small faint" style="text-transform:none;letter-spacing:0">金线＝平滑趋势　浅红底＝模型持仓　金方块＝你的真实交易</span></h4>
      <div id="c-price" class="chart"></div><div id="c-slope" class="chart slope"></div></section>
    <section class="panel"><h4><span>策略参数</span><span id="p-state" class="chip">当前生效参数</span></h4>
      <div class="params">
        <div><label>回归窗口（日）</label><input id="p-window" type="number" min="2" max="500" value="${inst.window}"></div>
        <div><label>EMA 平滑</label><input id="p-ema" type="number" min="1" max="100" value="${inst.ema}"></div>
        <div><label>买线 %/日</label><input id="p-buy" type="number" step="0.005" value="${inst.buy}"></div>
        <div><label>卖线 %/日</label><input id="p-sell" type="number" step="0.005" value="${inst.sell}"></div>
        <div><label>回测起点</label><input id="p-start" type="date" value="${inst.start_date}"></div>
      </div>
      <div class="param-actions"><button id="p-save" disabled>保存为生效参数</button><button class="ghost" id="p-reset">恢复</button>
        <span class="small faint">修改参数后图表和回测会实时预览，点保存后才影响每日信号</span></div>
    </section>
    <div class="cols">
      <section class="panel"><h4>策略 vs 买入持有</h4><div id="c-eq" class="chart eq"></div><div id="bt"></div><div id="periods"></div></section>
      <section class="panel"><h4>交易记录</h4><div class="tbl-wrap" id="trades"></div></section>
    </div>
    <section class="panel"><h4><span>参数优化 · 训练机</span><span style="display:flex;gap:8px;text-transform:none;letter-spacing:0">
      <select id="o-obj" style="width:auto">${Object.entries(OBJ).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
      <button class="sm" id="o-run">开始优化</button></span></h4><div id="opt"></div></section>
    <section class="panel"><h4><span>标的设置</span></h4>
      <div class="params">
        <div><label>名称</label><input id="s-name" value="${esc(inst.name)}"></div>
        <div><label>买入成本 bps</label><input id="s-bbps" type="number" value="${inst.buy_bps}"></div>
        <div><label>卖出成本 bps</label><input id="s-sbps" type="number" value="${inst.sell_bps}"></div>
        <div><label>每日推送</label><select id="s-en"><option value="1" ${inst.enabled ? "selected" : ""}>开启</option><option value="0" ${inst.enabled ? "" : "selected"}>暂停</option></select></div>
        <div><label>&nbsp;</label><div style="display:flex;gap:6px"><button class="sm" id="s-save">保存</button><button class="sm ghost" id="s-refresh">刷新数据</button><button class="sm ghost" id="s-del">删除</button></div></div>
      </div>
      <p class="note">数据源 ${esc(inst.provider)}:${esc(inst.code)} · 更新于 ${esc((inst.refreshed_at || "").replace("T", " ").slice(0, 16))} UTC</p></section>`;

  const pc = chartBase($("#c-price"), 420), sc = chartBase($("#c-slope"), 150), eq = chartBase($("#c-eq"), 240);
  pc.applyOptions({ timeScale: { visible: false } });
  D.series = {
    hold: pc.addHistogramSeries({ priceScaleId: "hold", color: "rgba(240,82,79,.07)", priceLineVisible: false, lastValueVisible: false }),
    candle: pc.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN }),
    trend: pc.addLineSeries({ color: GOLD, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
    slope: sc.addHistogramSeries({ priceLineVisible: false, priceFormat: { type: "custom", formatter: (x) => x.toFixed(2) + "%" } }),
    eq: eq.addLineSeries({ color: GOLD, lineWidth: 2, title: "策略" }),
    bh: eq.addLineSeries({ color: DIM, lineWidth: 1, lineStyle: 2, title: "持有" }),
  };
  pc.priceScale("hold").applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
  D.eq = eq;
  const sync = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange((r) => r && b.timeScale().setVisibleLogicalRange(r));
  sync(pc, sc); sync(sc, pc);
  drawCharts(v, inst);
  const n = v.bars.length;
  pc.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 260), to: n + 3 });
  renderBacktest(v);
  renderOpt(v.optimizations);

  for (const k of ["window", "ema", "buy", "sell", "start"]) $(`#p-${k}`).addEventListener("input", onParamInput);
  $("#p-save").onclick = () => saveParams().catch((e) => toast(e.message));
  $("#p-reset").onclick = () => setParams(inst);
  $("#o-run").onclick = async () => {
    try { await api(`/api/instruments/${sym}/optimize`, { method: "POST", body: { objective: $("#o-obj").value } }); const d = await api(`/api/instruments/${sym}`); renderOpt(d.optimizations); }
    catch (e) { toast(e.message); }
  };
  $("#s-save").onclick = async () => {
    await api(`/api/instruments/${sym}`, { method: "PATCH", body: { name: $("#s-name").value, buy_bps: +$("#s-bbps").value, sell_bps: +$("#s-sbps").value, enabled: $("#s-en").value === "1" } });
    toast("已保存"); route();
  };
  $("#s-refresh").onclick = async () => { $("#s-refresh").disabled = true; try { const r = await api(`/api/instruments/${sym}/refresh`, { method: "POST" }); toast(`已刷新 ${r.bars} 根日线`); route(); } catch (e) { toast(e.message); $("#s-refresh").disabled = false; } };
  $("#s-del").onclick = async () => { if (confirm(`删除 ${inst.name}？`)) { await api(`/api/instruments/${sym}`, { method: "DELETE" }); location.hash = "#/"; } };
}

/* ---------- add instrument ---------- */
let found = null;
$("#btn-add").onclick = () => { found = null; $("#add-result").innerHTML = ""; $("#add-confirm").disabled = true; $("#dlg-add").showModal(); $("#add-code").focus(); };
$("#add-lookup").onclick = async () => {
  try {
    found = await api(`/api/lookup?code=${encodeURIComponent($("#add-code").value)}`);
    $("#add-result").innerHTML = found.quote
      ? `<p><b>${esc(found.name || found.symbol)}</b> <span class="mono">${px(found.quote.price)}</span> <span class="faint small">${found.provider}:${found.code} · ${found.market}</span></p>`
      : `<p class="down">没有查到行情，检查代码是否正确</p>`;
    $("#add-confirm").disabled = !found.quote;
  } catch (e) { toast(e.message); }
};
$("#add-code").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#add-lookup").click(); } });
$("#add-confirm").onclick = async () => {
  $("#add-confirm").disabled = true;
  $("#add-result").insertAdjacentHTML("beforeend", '<p class="dim"><span class="spinner"></span> 下载历史数据…</p>');
  try {
    await api("/api/instruments", { method: "POST", body: { symbol: found.symbol.replace(/^(SH|SZ)/, ""), name: found.name || found.symbol, provider: found.provider, code: found.code, market: found.market, currency: found.currency, unit: found.unit } });
    $("#dlg-add").close(); toast("已添加，建议进入详情页跑一次参数优化"); route();
  } catch (e) { toast(e.message); $("#add-confirm").disabled = false; }
};

/* ---------- router ---------- */
async function route() {
  clearTimeout(D?.poll); D = null;
  const m = location.hash.match(/^#\/s\/(.+)$/);
  try { m ? await detail(decodeURIComponent(m[1])) : await overview(); }
  catch (e) { if (e.message !== "login") $("#view").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });
route();
