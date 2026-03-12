// analytics.js - Analytics and Chart.js rendering

const Analytics = (() => {
  let errorTypeChart = null;
  let accuracyChart = null;
  let speedChart = null;

  function classifyError(number, userAnswer) {
    const num = number;
    const ans = userAnswer;

    // Teen/tens confusion: e.g., 13 vs 30, 14 vs 40
    const teenTensMap = {
      13: 30, 14: 40, 15: 50, 16: 60, 17: 70, 18: 80, 19: 90,
      30: 13, 40: 14, 50: 15, 60: 16, 70: 17, 80: 18, 90: 19,
    };
    if (teenTensMap[num] === ans || teenTensMap[ans] === num) {
      return 'Teen/Tens Confusion';
    }

    // Similar sounding: off by a small digit difference
    const numStr = String(num);
    const ansStr = String(ans);
    if (numStr.length === ansStr.length && numStr.length > 0) {
      let diffCount = 0;
      for (let i = 0; i < numStr.length; i++) {
        if (numStr[i] !== ansStr[i]) diffCount++;
      }
      if (diffCount === 1) {
        return 'Similar Sounding';
      }
    }

    // Large number structure: wrong order of magnitude or structural error
    if (num >= 100 && ansStr.length !== numStr.length) {
      return 'Large Number Structure';
    }

    return 'Random Typo/Error';
  }

  function getErrorPatterns(history) {
    const errors = history.filter((r) => !r.correct);
    const counts = {};
    errors.forEach((r) => {
      const key = r.number;
      if (!counts[key]) {
        counts[key] = { number: key, errorCount: 0, answers: [] };
      }
      counts[key].errorCount++;
      counts[key].answers.push(r.userAnswer);
    });
    return Object.values(counts)
      .sort((a, b) => b.errorCount - a.errorCount)
      .slice(0, 10);
  }

  function getSlowNumbers(history) {
    const correct = history.filter((r) => r.correct);
    const byNumber = {};
    correct.forEach((r) => {
      if (!byNumber[r.number]) {
        byNumber[r.number] = { number: r.number, times: [] };
      }
      byNumber[r.number].times.push(r.reactionTimeMs);
    });
    return Object.values(byNumber)
      .map((entry) => ({
        number: entry.number,
        avgTime: Math.round(
          entry.times.reduce((a, b) => a + b, 0) / entry.times.length
        ),
        count: entry.times.length,
      }))
      .filter((e) => e.count >= 2)
      .sort((a, b) => b.avgTime - a.avgTime)
      .slice(0, 10);
  }

  function getErrorTypeDistribution(history) {
    const errors = history.filter((r) => !r.correct);
    const types = {};
    errors.forEach((r) => {
      const type = classifyError(r.number, r.userAnswer);
      types[type] = (types[type] || 0) + 1;
    });
    return types;
  }

  function getProgressOverTime(history) {
    if (history.length === 0) return { labels: [], accuracy: [], speed: [] };

    // Group by date
    const byDate = {};
    history.forEach((r) => {
      const date = new Date(r.timestamp).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
      });
      if (!byDate[date]) {
        byDate[date] = { correct: 0, total: 0, times: [] };
      }
      byDate[date].total++;
      if (r.correct) {
        byDate[date].correct++;
        byDate[date].times.push(r.reactionTimeMs);
      }
    });

    const labels = Object.keys(byDate);
    const accuracy = labels.map((d) =>
      Math.round((byDate[d].correct / byDate[d].total) * 100)
    );
    const speed = labels.map((d) => {
      const times = byDate[d].times;
      if (times.length === 0) return 0;
      return Math.round(times.reduce((a, b) => a + b, 0) / times.length);
    });

    return { labels, accuracy, speed };
  }

  function destroyCharts() {
    if (errorTypeChart) { errorTypeChart.destroy(); errorTypeChart = null; }
    if (accuracyChart) { accuracyChart.destroy(); accuracyChart = null; }
    if (speedChart) { speedChart.destroy(); speedChart = null; }
  }

  function render() {
    const history = Storage.getHistory();
    const emptyEl = document.getElementById('analytics-empty');
    const contentEl = document.getElementById('analytics-content');

    if (history.length === 0) {
      emptyEl.classList.remove('hidden');
      contentEl.classList.add('hidden');
      destroyCharts();
      return;
    }

    emptyEl.classList.add('hidden');
    contentEl.classList.remove('hidden');

    // Summary
    const correctCount = history.filter((r) => r.correct).length;
    const correctTimes = history.filter((r) => r.correct).map((r) => r.reactionTimeMs);
    document.getElementById('analytics-total').textContent = history.length;
    document.getElementById('analytics-accuracy').textContent =
      Math.round((correctCount / history.length) * 100) + '%';
    document.getElementById('analytics-avg-time').textContent =
      correctTimes.length > 0
        ? (correctTimes.reduce((a, b) => a + b, 0) / correctTimes.length / 1000).toFixed(1) + 's'
        : '--';

    // Error patterns
    renderErrorPatterns(history);

    // Slow numbers
    renderSlowNumbers(history);

    // Edited/hesitated answers
    renderEditedAnswers(history);

    // Charts
    destroyCharts();
    renderErrorTypeChart(history);
    renderAccuracyChart(history);
    renderSpeedChart(history);
  }

  function renderErrorPatterns(history) {
    const container = document.getElementById('error-patterns');
    const patterns = getErrorPatterns(history);

    if (patterns.length === 0) {
      container.innerHTML = '<p class="pattern-empty">No errors yet!</p>';
      return;
    }

    const maxErrors = patterns[0].errorCount;
    container.innerHTML = patterns
      .map((p) => {
        const recentAnswers = p.answers.slice(-3).join(', ');
        return `<div class="pattern-item">
          <span class="number">${p.number}</span>
          <div class="bar-container">
            <div class="bar error-bar" style="width: ${(p.errorCount / maxErrors) * 100}%"></div>
          </div>
          <span class="detail">${p.errorCount}x wrong (answered: <span class="wrong-answer">${recentAnswers}</span>)</span>
        </div>`;
      })
      .join('');
  }

  function renderSlowNumbers(history) {
    const container = document.getElementById('slow-numbers');
    const slow = getSlowNumbers(history);

    if (slow.length === 0) {
      container.innerHTML = '<p class="pattern-empty">Need more data (at least 2 correct answers per number)</p>';
      return;
    }

    const maxTime = slow[0].avgTime;
    container.innerHTML = slow
      .map((s) => {
        return `<div class="pattern-item">
          <span class="number">${s.number}</span>
          <div class="bar-container">
            <div class="bar slow-bar" style="width: ${(s.avgTime / maxTime) * 100}%"></div>
          </div>
          <span class="detail">${(s.avgTime / 1000).toFixed(1)}s avg (${s.count} answers)</span>
        </div>`;
      })
      .join('');
  }

  function renderEditedAnswers(history) {
    const container = document.getElementById('edited-answers');
    // Find records that have backspace actions in their edit log
    const edited = history.filter((r) => r.edits && r.edits.some((e) => e.action === 'backspace'));

    if (edited.length === 0) {
      container.innerHTML = '<p class="pattern-empty">No edited answers yet — great confidence!</p>';
      return;
    }

    // Group by number to find which numbers cause the most hesitation
    const byNumber = {};
    edited.forEach((r) => {
      const key = r.number;
      if (!byNumber[key]) {
        byNumber[key] = { number: key, editCount: 0, records: [] };
      }
      byNumber[key].editCount++;
      byNumber[key].records.push(r);
    });

    const sorted = Object.values(byNumber)
      .sort((a, b) => b.editCount - a.editCount)
      .slice(0, 10);

    const maxEdits = sorted[0].editCount;
    container.innerHTML = sorted
      .map((item) => {
        // Show what user first typed vs final answer for the most recent edit
        const recent = item.records[item.records.length - 1];
        const edits = recent.edits || [];
        const backspaces = edits.filter((e) => e.action === 'backspace').length;
        const firstValue = edits.length > 0 ? edits[0].value : '?';
        const correctStr = recent.correct ? '✅' : '❌';

        return `<div class="pattern-item">
          <span class="number">${item.number}</span>
          <div class="bar-container">
            <div class="bar slow-bar" style="width: ${(item.editCount / maxEdits) * 100}%"></div>
          </div>
          <span class="detail">${item.editCount}x edited, ${backspaces} ⌫ last time ${correctStr}</span>
        </div>`;
      })
      .join('');
  }

  function renderErrorTypeChart(history) {
    const types = getErrorTypeDistribution(history);
    const labels = Object.keys(types);
    const data = Object.values(types);

    if (labels.length === 0) {
      document.getElementById('error-type-chart').parentElement.style.display = 'none';
      return;
    }
    document.getElementById('error-type-chart').parentElement.style.display = '';

    const ctx = document.getElementById('error-type-chart').getContext('2d');
    errorTypeChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data,
          backgroundColor: ['#ef4444', '#f59e0b', '#6366f1', '#94a3b8'],
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom' },
        },
      },
    });
  }

  function renderAccuracyChart(history) {
    const progress = getProgressOverTime(history);
    if (progress.labels.length === 0) return;

    const ctx = document.getElementById('accuracy-chart').getContext('2d');
    accuracyChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: progress.labels,
        datasets: [{
          label: 'Accuracy %',
          data: progress.accuracy,
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34, 197, 94, 0.1)',
          fill: true,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        scales: {
          y: { min: 0, max: 100, ticks: { callback: (v) => v + '%' } },
        },
        plugins: { legend: { display: false } },
      },
    });
  }

  function renderSpeedChart(history) {
    const progress = getProgressOverTime(history);
    if (progress.labels.length === 0) return;

    const ctx = document.getElementById('speed-chart').getContext('2d');
    speedChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: progress.labels,
        datasets: [{
          label: 'Avg Reaction Time (ms)',
          data: progress.speed,
          borderColor: '#4a6cf7',
          backgroundColor: 'rgba(74, 108, 247, 0.1)',
          fill: true,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        scales: {
          y: { min: 0, ticks: { callback: (v) => (v / 1000).toFixed(1) + 's' } },
        },
        plugins: { legend: { display: false } },
      },
    });
  }

  return { render };
})();
