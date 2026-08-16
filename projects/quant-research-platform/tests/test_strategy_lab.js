const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../web/strategy-lab/core.js');

function rows(opens, closes = opens) {
  return opens.map((open, i) => ({ date: `2024-01-${String(i + 1).padStart(2, '0')}`, open, close: closes[i] }));
}

test('SMA positions use only closes available before the execution open', () => {
  const data = rows([10, 11, 12, 13, 14], [1, 2, 3, 4, 5]);
  const position = core.generatePositions(data, 'sma', { fast: 2, slow: 3 });
  assert.deepEqual(position.slice(0, 3), [0, 0, 0]);
  assert.equal(position[3], 1); // close[0..2] is available before open[3]
});

test('backtest earns open-to-next-open returns and counts one round trip', () => {
  const result = core.runBacktest(rows([100, 110, 121, 133.1]), [0, 1, 1, 0], 0);
  assert.ok(Math.abs(result.metrics.totalReturn - 0.21) < 1e-12);
  assert.equal(result.metrics.closedTrades, 1);
  assert.ok(Math.abs(result.rows[1].assetReturn - 0.1) < 1e-12);
});

test('one-way costs apply to every exposure change', () => {
  const result = core.runBacktest(rows([100, 110, 121, 133.1]), [0, 1, 1, 0], 5);
  assert.ok(result.metrics.totalReturn < 0.21);
  assert.ok(Math.abs(result.metrics.turnover - 2) < 1e-12);
  assert.ok(Math.abs(result.metrics.costPaid - 0.001) < 1e-12);
});

test('persistence rule enters on the next open and holds the frozen horizon', () => {
  const opens = [100, 101, 102.2, 103, 104.5, 106, 107, 108.5, 110, 111.5, 113, 114.5];
  const position = core.generatePositions(rows(opens), 'persistence', {
    trendLookback: 3,
    trendThreshold: 0.2,
    volShort: 3,
    volLong: 5,
    volRatioThreshold: 2,
    holdDays: 2,
  });
  const first = position.findIndex((value) => value > 0);
  assert.ok(first >= 6);
  assert.equal(position[first - 1], 0);
  assert.equal(position[first], 1);
  assert.equal(position[first + 1], 1);
});

test('evaluation compares the strategy with an aligned buy-and-hold benchmark', () => {
  const data = rows(Array.from({ length: 260 }, (_, i) => 100 + i * 0.2));
  const result = core.evaluate(data, { strategy: 'trend', trendWindow: 50, costBps: 5 });
  assert.equal(result.strategy.rows.length, data.length);
  assert.equal(result.benchmark.rows.length, data.length);
  assert.equal(result.strategy.rows[0].date, result.benchmark.rows[0].date);
  assert.ok(result.benchmark.metrics.totalReturn > 0);
});

test('invalid parameter combinations fail closed', () => {
  const data = rows(Array.from({ length: 20 }, (_, i) => 100 + i));
  assert.throws(() => core.generatePositions(data, 'sma', { fast: 20, slow: 10 }), /fast.*slow/i);
  assert.throws(() => core.runBacktest(data, new Array(19).fill(1), 5), /length/i);
  assert.throws(() => core.runBacktest(data, [0, Number.NaN, ...new Array(18).fill(0)], 5), /position/i);
  assert.throws(() => core.generatePositions(data, 'persistence', { trendLookback: 5, volShort: 80, volLong: 60, holdDays: 5 }), /volShort.*volLong/i);
  assert.throws(() => core.generatePositions(data, 'vote', { voteShort: 100, voteMedium: 80, voteLong: 200, votesRequired: 2 }), /vote.*order/i);
});
