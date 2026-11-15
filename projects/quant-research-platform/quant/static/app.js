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

function card(item, i) {
  const inst = item.instrument;
  if (!item.ready) return `<article class="card" style="animation-delay:${i * 60}ms"><h2>${esc(inst.name)}</h2><p class="dim">${inst.refresh_error ? "数据获取失败：" + esc(inst.refresh_error) : "正在获取历史数据…"}</p></article>`;
  const s = item.signal;
  return `<article class="card ${s.position ? "held" : ""} act-${s.action}" style="animation-delay:${i * 60}ms" onclick="location.hash='#/s/${encodeURIComponent(inst.symbol)}'">
    <div class="card-head">
      <div><h2>${esc(inst.name)}</h2><div class="code">${esc(inst.symbol)} · ${s.date}${s.provisional ? " · 临时收盘" : ""}</div></div>
      <span class="badge ${s.action}">${s.action === "BUY" || s.action === "SELL" ? "明日开盘" : ""}${ACT[s.action]}</span>
    </div>
    <div class="price-row"><span class="price">${px(s.close)}</span><span class="chg ${cls(s.change)}">${pct(s.change, 2)}</span>
      <span class="small faint" style="margin-left:auto">${s.position ? '<span class="up">● 持有中</span>' : "○ 空仓"}</span></div>
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

async function overview() {
  const view = $("#view");
  const { items, events } = await api("/api/overview");
  const ready = items.filter((x) => x.ready);
  const acts = ready.filter((x) => ["BUY", "SELL"].includes(x.signal.action));
  const held = ready.filter((x) => x.signal.position);
  const names = Object.fromEntries(items.map((x) => [x.instrument.symbol, x.instrument.name]));
  const latest = ready.map((x) => x.signal.date).sort().at(-1) || "";
  const headline = acts.length
    ? `今日 <em>${acts.length}</em> 个标的需操作`
    : `今日无需操作`;
  const sub = acts.length
    ? acts.map((x) => `${x.instrument.name}：下一交易日开盘${ACT[x.signal.action]}`).join("；")
    : `持有 ${held.length} 个 · 空仓 ${ready.length - held.length} 个。卡片下方是距离触发价的涨跌幅，越接近 0 越可能在下个收盘出信号。`;
  view.innerHTML = `
    <section class="verdict">
      <div><div class="eyebrow">收盘信号 · ${latest}</div><h1>${headline}</h1><p>${esc(sub)}</p></div>
      <div class="legend"><span><i style="background:${UP}"></i>买入 / 持有</span><span><i style="background:${DOWN}"></i>卖出</span><span><i style="background:${GOLD}"></i>触发线</span></div>
    </section>
    <section class="grid">${items.map(card).join("")}</section>
    <div class="section-title"><h3>信号日志</h3><span class="small faint">收盘信号 · 盘中预警 · 参数变更</span></div>
    <section class="feed">${events.length ? events.map((e) => feedRow(e, names)).join("") : '<div class="empty">暂无记录，首次收盘任务运行后出现</div>'}</section>`;
  if (items.some((x) => !x.ready && !x.instrument.refresh_error)) setTimeout(() => route(), 4000);
}

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
  s.candle.setMarkers(markers(dates, v.position).filter((m) => m.time >= dates[v.start_index]));
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

function renderBacktest(v) {
  const st = v.backtest.strategy;
  $("#bt").innerHTML = metricsTable([["趋势策略", st], ["买入持有", v.backtest.buy_hold]]) +
    `<p class="note">自 <b>${v.backtest.dates[0]}</b> 起回测 · 完成 <b>${st.trades}</b> 笔交易 · 胜率 <b>${st.win_rate == null ? "—" : pct(st.win_rate, 0)}</b> · 持仓时间占比 <b>${pct(st.exposure, 0)}</b>。收盘出信号、次日开盘成交，已扣除买卖成本。</p>`;
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
    <div class="cols" style="margin-top:12px">
      <div><h4>各阶段所选参数</h4><table class="metrics"><thead><tr><th>检验区间</th><th>参数</th><th>策略</th><th>持有</th></tr></thead><tbody>
        ${wf.folds.map((f) => `<tr><td>${f.test_start.slice(0, 7)}~${f.test_end.slice(0, 7)}</td><td>${f.params.window}/${f.params.ema}/${f.params.buy}/${f.params.sell}</td><td class="${cls(f.strategy.total_return)}">${pct(f.strategy.total_return)}</td><td class="${cls(f.buy_hold.total_return)}">${pct(f.buy_hold.total_return)}</td></tr>`).join("")}
      </tbody></table></div>
      <div><h4>参数稳定性（窗口 × EMA）</h4>${heatmap(r.heatmap)}</div>
    </div>`;
  $("#opt-try").onclick = () => { setParams(rec); window.scrollTo({ top: 0, behavior: "smooth" }); toast("已载入推荐参数，图表为预览效果"); };
  $("#opt-apply").onclick = async () => { if (!confirm("采用推荐参数？之后的每日信号将按新参数计算。")) return; await api(`/api/instruments/${D.sym}/apply`, { method: "POST", body: rec }); toast("已采用"); route(); };
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
        <div class="stat"><label>模型仓位</label><b class="${s.position ? "up" : ""}">${s.position ? "持有" : "空仓"}</b></div>
        <div class="stat"><label>趋势斜率</label><b class="${cls(s.slope)}">${sgn(s.slope)}%</b></div>
        <div class="stat"><label>${s.buy_trigger ? "买入触发" : "卖出触发"}</label><b class="${s.buy_trigger ? "up" : "down"}">${px(s.buy_trigger || s.sell_trigger)}</b></div>
        <div class="stat"><label>距触发</label><b>${s.buy_trigger || s.sell_trigger ? pct((s.buy_trigger || s.sell_trigger) / s.close - 1, 2) : "—"}</b></div>
      </div>
    </section>
    <section class="panel"><h4><span>价格 · 趋势线 · 买卖点</span><span class="small faint" style="text-transform:none;letter-spacing:0">金线＝平滑趋势　浅红底＝持仓区间</span></h4>
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
      <section class="panel"><h4>策略 vs 买入持有</h4><div id="c-eq" class="chart eq"></div><div id="bt"></div></section>
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
