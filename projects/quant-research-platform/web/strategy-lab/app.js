(() => {
  'use strict';
  const core = window.GoldLabCore;
  const source = window.GOLD_LAB_DATA;
  const $ = (id) => document.getElementById(id);
  const controls = ['symbol','years','strategy','costBps','fast','slow','entryWindow','exitWindow','trendWindow','momentumLookback','voteShort','voteMedium','voteLong','votesRequired','volWindow','targetVol','trendLookback','trendThreshold','volShort','volLong','volRatioThreshold','holdDays'];
  const defaults = { symbol:'GLD', years:'3', strategy:'persistence', costBps:5, fast:50, slow:200, entryWindow:55, exitWindow:20, trendWindow:200, momentumLookback:252, voteShort:63, voteMedium:126, voteLong:252, votesRequired:2, volWindow:60, targetVol:.10, trendLookback:20, trendThreshold:1, volShort:20, volLong:252, volRatioThreshold:1, holdDays:20 };
  let timer;

  const percent = (value, digits=1) => `${(Number(value)*100).toFixed(digits)}%`;
  const signedPercent = (value, digits=1) => `${Number(value)>=0?'+':''}${percent(value,digits)}`;
  const number = (id) => Number($(id).value);
  const currentConfig = () => ({
    symbol:$('symbol').value, years:$('years').value, strategy:$('strategy').value,
    costBps:number('costBps'), fast:number('fast'), slow:number('slow'), entryWindow:number('entryWindow'), exitWindow:number('exitWindow'), trendWindow:number('trendWindow'), momentumLookback:number('momentumLookback'), voteShort:number('voteShort'), voteMedium:number('voteMedium'), voteLong:number('voteLong'), votesRequired:number('votesRequired'), volWindow:number('volWindow'), targetVol:number('targetVol'), trendLookback:number('trendLookback'), trendThreshold:number('trendThreshold'), volShort:number('volShort'), volLong:number('volLong'), volRatioThreshold:number('volRatioThreshold'), holdDays:number('holdDays')
  });

  function updateOutputs() {
    document.querySelectorAll('.range-row input').forEach((input) => {
      const output = input.parentElement.querySelector('output');
      let value = Number(input.value);
      if (input.id === 'targetVol') output.textContent = percent(value,0);
      else if (['trendThreshold','volRatioThreshold'].includes(input.id)) output.textContent = value.toFixed(input.id==='volRatioThreshold'?2:1);
      else output.textContent = String(value);
    });
  }

  function updateGroups() {
    const strategy = $('strategy').value;
    document.querySelectorAll('.param-group').forEach((group) => {
      group.hidden = !group.dataset.for.split(' ').includes(strategy);
    });
  }

  function saveConfig() {
    try { localStorage.setItem('gold-lab-config-v1', JSON.stringify(currentConfig())); } catch (_) {}
  }

  function loadConfig() {
    let config = defaults;
    try { config = {...defaults, ...JSON.parse(localStorage.getItem('gold-lab-config-v1') || '{}')}; } catch (_) {}
    controls.forEach((id) => { if ($(id) && config[id] != null) $(id).value = config[id]; });
    updateOutputs(); updateGroups();
  }

  function metricCard(label, value, benchmark, higherIsBetter=true) {
    const difference = value.raw - benchmark.raw;
    const good = higherIsBetter ? difference >= 0 : difference <= 0;
    return `<article class="metric ${good?'good':'bad'}"><span>${label}</span><strong>${value.text}</strong><small>持有 ${benchmark.text} · 差 ${difference>=0?'+':''}${value.diff(difference)}</small></article>`;
  }

  function chartSvg(series, options={}) {
    const width=900, height=options.height||300, pad={l:54,r:18,t:16,b:32};
    const all = series.flatMap((item) => item.values.filter(Number.isFinite));
    let min=Math.min(...all), max=Math.max(...all);
    if (options.includeZero) { min=Math.min(min,0); max=Math.max(max,0); }
    if (max===min) { max+=1; min-=1; }
    const x=(i,n)=>pad.l+(width-pad.l-pad.r)*(n<=1?0:i/(n-1));
    const y=(v)=>pad.t+(height-pad.t-pad.b)*(max-v)/(max-min);
    const paths=series.map((item) => {
      const n=item.values.length; const stride=Math.max(1,Math.floor(n/600)); const points=[];
      for(let i=0;i<n;i+=stride) points.push(`${x(i,n).toFixed(1)},${y(item.values[i]).toFixed(1)}`);
      if ((n-1)%stride) points.push(`${x(n-1,n).toFixed(1)},${y(item.values[n-1]).toFixed(1)}`);
      return `<polyline class="${item.className}" points="${points.join(' ')}"/>`;
    }).join('');
    let grid='';
    for(let i=0;i<5;i++){const value=max-(max-min)*i/4;const yy=y(value);grid+=`<line class="gridline" x1="${pad.l}" y1="${yy}" x2="${width-pad.r}" y2="${yy}"/><text x="${pad.l-8}" y="${yy+4}" text-anchor="end">${options.percent?percent(value,0):value.toFixed(value<10?1:0)}</text>`;}
    const labels=options.labels||['',''];
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${options.label||'chart'}">${grid}${paths}<text x="${pad.l}" y="${height-8}">${labels[0]}</text><text x="${width-pad.r}" y="${height-8}" text-anchor="end">${labels[1]}</text></svg>`;
  }

  function yearlyRows(strategyRows, benchmarkRows) {
    const buckets = new Map();
    for (let i=0;i<strategyRows.length;i++) {
      const year=strategyRows[i].date.slice(0,4);
      if(!buckets.has(year)) buckets.set(year,{s:1,b:1});
      const item=buckets.get(year); item.s*=1+strategyRows[i].netReturn; item.b*=1+benchmarkRows[i].netReturn;
    }
    return [...buckets.entries()].map(([year,item])=>{const s=item.s-1,b=item.b-1,d=s-b;return `<tr><td>${year}</td><td>${signedPercent(s,1)}</td><td>${signedPercent(b,1)}</td><td>${signedPercent(d,1)}</td></tr>`;}).join('');
  }

  function statusText(config, position) {
    if (config.strategy==='buyhold') return '始终保持100%黄金仓位。';
    if (config.strategy==='persistence') return position>0 ? `候选事件处于持有期：趋势阈值 ${config.trendThreshold.toFixed(1)}，波动比上限 ${config.volRatioThreshold.toFixed(2)}，持有 ${config.holdDays} 日。` : '当前没有满足“强趋势＋低波动”条件的持有事件。';
    if (config.strategy==='trendVol') return position>0 ? `趋势成立，按目标波动率缩放为 ${percent(position,0)} 仓位。` : '长期趋势未满足，当前空仓。';
    return position>0 ? '当前规则给出持有状态。' : '当前规则给出空仓状态。';
  }

  function render() {
    clearTimeout(timer);
    $('error').hidden=true;
    try {
      const config=currentConfig();
      const full=source.symbols[config.symbol].rows;
      const sliced=core.sliceForYears(full,config.years);
      const start=full.length-sliced.length;
      const fullPositions=core.generatePositions(full,config.strategy,config);
      const positions=fullPositions.slice(start);
      const strategy=core.runBacktest(sliced,positions,config.costBps);
      const benchmark=core.runBacktest(sliced,new Array(sliced.length).fill(1),config.costBps);
      const sm=strategy.metrics,bm=benchmark.metrics;
      $('metrics').innerHTML=[
        metricCard('年化收益',{raw:sm.cagr,text:percent(sm.cagr,2),diff:(v)=>percent(v,2)},{raw:bm.cagr,text:percent(bm.cagr,2)},true),
        metricCard('累计收益',{raw:sm.totalReturn,text:percent(sm.totalReturn,1),diff:(v)=>percent(v,1)},{raw:bm.totalReturn,text:percent(bm.totalReturn,1)},true),
        metricCard('最大回撤',{raw:sm.maxDrawdown,text:percent(sm.maxDrawdown,1),diff:(v)=>percent(v,1)},{raw:bm.maxDrawdown,text:percent(bm.maxDrawdown,1)},true),
        metricCard('Sharpe',{raw:sm.sharpe,text:sm.sharpe.toFixed(2),diff:(v)=>v.toFixed(2)},{raw:bm.sharpe,text:bm.sharpe.toFixed(2)},true),
        metricCard('已完成交易',{raw:sm.closedTrades,text:String(sm.closedTrades),diff:(v)=>String(Math.round(v))},{raw:bm.closedTrades,text:String(bm.closedTrades)},false),
        metricCard('平均仓位',{raw:sm.exposure,text:percent(sm.exposure,0),diff:(v)=>percent(v,0)},{raw:bm.exposure,text:percent(bm.exposure,0)},true),
      ].join('');
      const lastPosition=fullPositions[fullPositions.length-1];
      $('current-position').textContent=lastPosition>0 ? (lastPosition>=.995?'持有':`${percent(lastPosition,0)}仓位`) : '空仓';
      $('current-explanation').textContent=statusText(config,lastPosition);
      const dateLabels=[sliced[0].date,sliced[sliced.length-1].date];
      $('equity-chart').innerHTML=chartSvg([{values:strategy.rows.map(r=>r.equity),className:'strategy-line'},{values:benchmark.rows.map(r=>r.equity),className:'benchmark-line'}],{labels:dateLabels,label:'equity curve'});
      $('drawdown-chart').innerHTML=chartSvg([{values:strategy.rows.map(r=>r.drawdown),className:'strategy-line'},{values:benchmark.rows.map(r=>r.drawdown),className:'benchmark-line'}],{labels:dateLabels,label:'drawdown curve',percent:true,includeZero:true,height:210});
      $('yearly').innerHTML=yearlyRows(strategy.rows,benchmark.rows);
      $('data-status').textContent=`数据 ${source.symbols[config.symbol].start}～${source.symbols[config.symbol].end} · ${source.symbols[config.symbol].rows.length.toLocaleString()}行`;
      saveConfig();
    } catch (error) {
      $('error').textContent=error.message||String(error); $('error').hidden=false;
      $('metrics').innerHTML='';
      $('equity-chart').innerHTML='';
      $('drawdown-chart').innerHTML='';
      $('yearly').innerHTML='';
      $('current-position').textContent='参数错误';
      $('current-explanation').textContent='请修正左侧参数后重新计算。';
    }
  }

  function scheduleRender(){clearTimeout(timer);timer=setTimeout(render,40);}

  function applyPreset(name){
    const presets={buyhold:{strategy:'buyhold'},trend:{strategy:'trend',trendWindow:200},trendVol:{strategy:'trendVol',trendWindow:200,volWindow:60,targetVol:.10},persistence:{strategy:'persistence',trendLookback:20,trendThreshold:1,volShort:20,volLong:252,volRatioThreshold:1,holdDays:20}};
    Object.entries(presets[name]).forEach(([key,value])=>{if($(key))$(key).value=value;});updateOutputs();updateGroups();render();
  }

  loadConfig();
  controls.forEach((id)=>$(id).addEventListener('input',()=>{updateOutputs();updateGroups();scheduleRender();}));
  $('reset').addEventListener('click',()=>{controls.forEach((id)=>{if(defaults[id]!=null)$(id).value=defaults[id];});updateOutputs();updateGroups();render();});
  document.querySelectorAll('[data-preset]').forEach((button)=>button.addEventListener('click',()=>applyPreset(button.dataset.preset)));
  render();
})();
