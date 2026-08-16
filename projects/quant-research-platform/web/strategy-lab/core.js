(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GoldLabCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const mean = (values) => values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
  const std = (values) => {
    if (!values.length) return 0;
    const m = mean(values);
    return Math.sqrt(mean(values.map((value) => (value - m) ** 2)));
  };
  const sumSlice = (prefix, start, end) => prefix[end] - prefix[start];
  const prefixSums = (values) => {
    const result = [0];
    for (const value of values) result.push(result[result.length - 1] + value);
    return result;
  };
  const rollingMean = (prefix, endExclusive, window) => sumSlice(prefix, endExclusive - window, endExclusive) / window;
  const positiveInteger = (value, name) => {
    const number = Number(value);
    if (!Number.isInteger(number) || number <= 0) throw new Error(`${name} must be a positive integer`);
    return number;
  };

  function generatePositions(data, strategy, params = {}) {
    if (!Array.isArray(data) || data.length < 2) throw new Error('data must contain at least two rows');
    const opens = data.map((row) => Number(row.open));
    const closes = data.map((row) => Number(row.close));
    if (opens.some((value) => !Number.isFinite(value) || value <= 0) || closes.some((value) => !Number.isFinite(value) || value <= 0)) {
      throw new Error('prices must be finite and positive');
    }
    const n = data.length;
    const position = new Array(n).fill(0);
    const closePrefix = prefixSums(closes);

    if (strategy === 'buyhold') return new Array(n).fill(1);

    if (strategy === 'sma') {
      const fast = positiveInteger(params.fast ?? 50, 'fast');
      const slow = positiveInteger(params.slow ?? 200, 'slow');
      if (fast >= slow) throw new Error('fast must be less than slow');
      for (let i = slow; i < n; i++) {
        position[i] = rollingMean(closePrefix, i, fast) > rollingMean(closePrefix, i, slow) ? 1 : 0;
      }
      return position;
    }

    if (strategy === 'trend') {
      const window = positiveInteger(params.trendWindow ?? 200, 'trendWindow');
      for (let i = window; i < n; i++) {
        position[i] = closes[i - 1] > rollingMean(closePrefix, i, window) ? 1 : 0;
      }
      return position;
    }

    if (strategy === 'momentum') {
      const lookback = positiveInteger(params.momentumLookback ?? 252, 'momentumLookback');
      for (let i = lookback + 1; i < n; i++) position[i] = closes[i - 1] > closes[i - 1 - lookback] ? 1 : 0;
      return position;
    }

    if (strategy === 'vote') {
      const lookbacks = [params.voteShort ?? 63, params.voteMedium ?? 126, params.voteLong ?? 252].map((value, index) => positiveInteger(value, `voteLookback${index + 1}`));
      const votesRequired = positiveInteger(params.votesRequired ?? 2, 'votesRequired');
      if (!(lookbacks[0] < lookbacks[1] && lookbacks[1] < lookbacks[2])) throw new Error('vote horizons must be in ascending order');
      if (votesRequired > lookbacks.length) throw new Error('votesRequired exceeds lookback count');
      const warmup = Math.max(...lookbacks) + 1;
      for (let i = warmup; i < n; i++) {
        const votes = lookbacks.filter((lookback) => closes[i - 1] > closes[i - 1 - lookback]).length;
        position[i] = votes >= votesRequired ? 1 : 0;
      }
      return position;
    }

    if (strategy === 'donchian') {
      const entry = positiveInteger(params.entryWindow ?? 55, 'entryWindow');
      const exit = positiveInteger(params.exitWindow ?? 20, 'exitWindow');
      let state = 0;
      for (let i = 1; i < n; i++) {
        const j = i - 1;
        if (j >= entry) {
          let upper = -Infinity;
          for (let k = j - entry; k < j; k++) upper = Math.max(upper, closes[k]);
          if (closes[j] > upper) state = 1;
        }
        if (j >= exit) {
          let lower = Infinity;
          for (let k = j - exit; k < j; k++) lower = Math.min(lower, closes[k]);
          if (closes[j] < lower) state = 0;
        }
        position[i] = state;
      }
      return position;
    }

    if (strategy === 'trendVol') {
      const trendWindow = positiveInteger(params.trendWindow ?? 200, 'trendWindow');
      const volWindow = positiveInteger(params.volWindow ?? 60, 'volWindow');
      const targetVol = Number(params.targetVol ?? 0.10);
      if (!(targetVol > 0 && targetVol <= 1)) throw new Error('targetVol must be between zero and one');
      const returns = opens.map((value, i) => i ? value / opens[i - 1] - 1 : 0);
      for (let i = Math.max(trendWindow, volWindow + 1); i < n; i++) {
        const trending = closes[i - 1] > rollingMean(closePrefix, i, trendWindow);
        const realized = std(returns.slice(i - volWindow, i)) * Math.sqrt(252);
        position[i] = trending && realized > 0 ? clamp(targetVol / realized, 0, 1) : 0;
      }
      return position;
    }

    if (strategy === 'persistence') {
      const lookback = positiveInteger(params.trendLookback ?? 20, 'trendLookback');
      const volShort = positiveInteger(params.volShort ?? 20, 'volShort');
      const volLong = positiveInteger(params.volLong ?? 252, 'volLong');
      const holdDays = positiveInteger(params.holdDays ?? 20, 'holdDays');
      const threshold = Number(params.trendThreshold ?? 1);
      const ratioThreshold = Number(params.volRatioThreshold ?? 1);
      if (volShort >= volLong) throw new Error('volShort must be less than volLong');
      if (!(threshold > 0) || !(ratioThreshold > 0)) throw new Error('persistence thresholds must be positive');
      const returns = opens.map((value, i) => i ? value / opens[i - 1] - 1 : 0);
      let remaining = 0;
      const warmup = Math.max(lookback + 1, volShort + 1, volLong + 1);
      for (let i = 1; i < n; i++) {
        if (remaining > 0) {
          position[i] = 1;
          remaining -= 1;
          continue;
        }
        const j = i - 1;
        if (j < warmup) continue;
        const localVol = std(returns.slice(j - lookback + 1, j + 1));
        const shortVol = std(returns.slice(j - volShort + 1, j + 1));
        const longVol = std(returns.slice(j - volLong + 1, j + 1));
        const pastReturn = opens[j] / opens[j - lookback] - 1;
        const score = localVol > 0 ? pastReturn / (localVol * Math.sqrt(lookback)) : 0;
        const ratio = longVol > 0 ? shortVol / longVol : Infinity;
        if (score > threshold && ratio <= ratioThreshold) {
          position[i] = 1;
          remaining = holdDays - 1;
        }
      }
      return position;
    }

    throw new Error(`unknown strategy: ${strategy}`);
  }

  function runBacktest(data, positions, costBps = 5) {
    if (data.length !== positions.length) throw new Error('data and position length must match');
    if (positions.some((value) => !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 1)) throw new Error('positions must be finite numbers between zero and one');
    if (data.some((row) => !Number.isFinite(Number(row.open)) || Number(row.open) <= 0)) throw new Error('open prices must be finite and positive');
    const rate = Number(costBps) / 10000;
    if (!Number.isFinite(rate) || rate < 0) throw new Error('costBps must be non-negative');
    let equity = 1;
    let grossEquity = 1;
    let peak = 1;
    let maxDrawdown = 0;
    let previous = 0;
    let turnoverTotal = 0;
    let costPaid = 0;
    let closedTrades = 0;
    let active = false;
    const returns = [];
    const rows = [];
    for (let i = 0; i < data.length; i++) {
      const position = Number(positions[i]);
      const turnover = Math.abs(position - previous);
      const assetReturn = i + 1 < data.length ? Number(data[i + 1].open) / Number(data[i].open) - 1 : 0;
      const grossReturn = position * assetReturn;
      const cost = turnover * rate;
      const netReturn = grossReturn - cost;
      equity *= 1 + netReturn;
      grossEquity *= 1 + grossReturn;
      peak = Math.max(peak, equity);
      maxDrawdown = Math.min(maxDrawdown, equity / peak - 1);
      if (!active && position > 0) active = true;
      if (active && position === 0 && previous > 0) { closedTrades += 1; active = false; }
      turnoverTotal += turnover;
      costPaid += cost;
      returns.push(netReturn);
      rows.push({ date: data[i].date, open: Number(data[i].open), close: Number(data[i].close), position, assetReturn, grossReturn, cost, netReturn, equity, grossEquity, drawdown: equity / peak - 1 });
      previous = position;
    }
    const start = new Date(data[0].date + 'T00:00:00Z');
    const end = new Date(data[data.length - 1].date + 'T00:00:00Z');
    const calendarYears = Math.max((end - start) / 86400000 / 365.2425, (data.length - 1) / 252, 1 / 252);
    const dailyStd = std(returns);
    const totalReturn = equity - 1;
    const cagr = equity > 0 ? equity ** (1 / calendarYears) - 1 : -1;
    const metrics = {
      totalReturn,
      cagr,
      maxDrawdown,
      sharpe: dailyStd > 0 ? mean(returns) / dailyStd * Math.sqrt(252) : 0,
      annualVolatility: dailyStd * Math.sqrt(252),
      exposure: mean(rows.map((row) => row.position)),
      turnover: turnoverTotal,
      costPaid,
      closedTrades,
      openTrades: active ? 1 : 0,
      finalEquity: equity,
    };
    return { rows, metrics };
  }

  function evaluate(data, config) {
    const positions = generatePositions(data, config.strategy, config);
    const strategy = runBacktest(data, positions, config.costBps ?? 5);
    const benchmark = runBacktest(data, new Array(data.length).fill(1), config.costBps ?? 5);
    return { strategy, benchmark, positions };
  }

  function sliceForYears(data, years) {
    if (years === 'all' || years == null) return data.slice();
    const count = Number(years);
    const latest = new Date(data[data.length - 1].date + 'T00:00:00Z');
    const cutoff = new Date(latest);
    cutoff.setUTCFullYear(cutoff.getUTCFullYear() - count);
    const index = data.findIndex((row) => new Date(row.date + 'T00:00:00Z') >= cutoff);
    return data.slice(index < 0 ? 0 : index);
  }

  return { generatePositions, runBacktest, evaluate, sliceForYears, mean, std };
});
