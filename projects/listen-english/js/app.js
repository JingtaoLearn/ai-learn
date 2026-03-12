// app.js - Main application logic

const App = (() => {
  // State
  let currentNumber = null;
  let isPlaying = false;
  let session = {
    total: 0,
    correct: 0,
    wrong: 0,
    streak: 0,
    reactionTimes: [],
  };

  // DOM elements
  const els = {};

  function init() {
    cacheElements();
    loadSettings();
    bindEvents();
    TTS.init();
    updateStats();
  }

  function cacheElements() {
    els.minNumber = document.getElementById('min-number');
    els.maxNumber = document.getElementById('max-number');
    els.autoPlayDelay = document.getElementById('auto-play-delay');
    els.playBtn = document.getElementById('play-btn');
    els.replayBtn = document.getElementById('replay-btn');
    els.answerInput = document.getElementById('answer-input');
    els.feedback = document.getElementById('feedback');
    els.reactionTime = document.getElementById('reaction-time');
    els.statTotal = document.getElementById('stat-total');
    els.statCorrect = document.getElementById('stat-correct');
    els.statWrong = document.getElementById('stat-wrong');
    els.statAccuracy = document.getElementById('stat-accuracy');
    els.statAvgTime = document.getElementById('stat-avg-time');
    els.statStreak = document.getElementById('stat-streak');
    els.clearHistoryBtn = document.getElementById('clear-history-btn');
    els.navTabs = document.querySelectorAll('.nav-tab');
    els.tabContents = document.querySelectorAll('.tab-content');
    els.presetBtns = document.querySelectorAll('.preset-btn');
  }

  function loadSettings() {
    const settings = Storage.getSettings();
    els.minNumber.value = settings.min;
    els.maxNumber.value = settings.max;
    els.autoPlayDelay.value = settings.autoPlayDelay;
  }

  function saveSettings() {
    Storage.saveSettings({
      min: parseInt(els.minNumber.value, 10) || 1,
      max: parseInt(els.maxNumber.value, 10) || 100,
      autoPlayDelay: parseFloat(els.autoPlayDelay.value) || 1.5,
    });
  }

  function bindEvents() {
    // Play button
    els.playBtn.addEventListener('click', playNumber);

    // Replay button
    els.replayBtn.addEventListener('click', replayNumber);

    // Answer input — Enter to submit
    els.answerInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitAnswer();
      }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      // Don't trigger shortcuts when typing in inputs (except answer-input Enter handled above)
      if (e.target.tagName === 'INPUT' && e.target !== els.answerInput) return;
      if (e.target === els.answerInput && e.key !== ' ') return;

      if (e.key === ' ' && e.target !== els.answerInput) {
        e.preventDefault();
        playNumber();
      } else if ((e.key === 'r' || e.key === 'R') && e.target !== els.answerInput) {
        e.preventDefault();
        replayNumber();
      }
    });

    // Preset buttons
    els.presetBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        els.minNumber.value = btn.dataset.min;
        els.maxNumber.value = btn.dataset.max;
        saveSettings();
      });
    });

    // Settings change
    els.minNumber.addEventListener('change', saveSettings);
    els.maxNumber.addEventListener('change', saveSettings);
    els.autoPlayDelay.addEventListener('change', saveSettings);

    // Tab switching
    els.navTabs.forEach((tab) => {
      tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // Clear history
    els.clearHistoryBtn.addEventListener('click', () => {
      if (confirm('Clear all history? This cannot be undone.')) {
        Storage.clearHistory();
        Analytics.render();
      }
    });
  }

  function switchTab(tabName) {
    els.navTabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === tabName));
    els.tabContents.forEach((c) => {
      c.classList.toggle('active', c.id === tabName + '-tab');
    });
    if (tabName === 'analytics') {
      Analytics.render();
    }
  }

  function getRandomNumber() {
    const min = parseInt(els.minNumber.value, 10) || 1;
    const max = parseInt(els.maxNumber.value, 10) || 100;
    const lo = Math.min(min, max);
    const hi = Math.max(min, max);
    return Math.floor(Math.random() * (hi - lo + 1)) + lo;
  }

  async function playNumber() {
    if (isPlaying) return;
    isPlaying = true;

    // Generate new number
    currentNumber = getRandomNumber();

    // Reset UI
    els.answerInput.value = '';
    els.feedback.classList.add('hidden');
    els.feedback.className = 'feedback hidden';
    els.reactionTime.classList.add('hidden');
    els.answerInput.disabled = false;
    els.replayBtn.disabled = false;
    els.playBtn.disabled = true;

    Timer.reset();

    try {
      // Speak the number, start timer when speech ends
      await TTS.speak(currentNumber);
      Timer.start();
      els.answerInput.focus();
    } catch (err) {
      console.error('TTS error:', err);
    }

    isPlaying = false;
    els.playBtn.disabled = false;
  }

  async function replayNumber() {
    if (currentNumber === null || isPlaying) return;
    isPlaying = true;
    els.playBtn.disabled = true;

    Timer.reset();

    try {
      await TTS.speak(currentNumber);
      Timer.start();
      els.answerInput.focus();
    } catch (err) {
      console.error('TTS replay error:', err);
    }

    isPlaying = false;
    els.playBtn.disabled = false;
  }

  function submitAnswer() {
    if (currentNumber === null || isPlaying) return;

    const raw = els.answerInput.value.trim();
    if (raw === '') return;

    const userAnswer = parseInt(raw, 10);
    if (isNaN(userAnswer)) return;

    const reactionTimeMs = Timer.stop();
    const correct = userAnswer === currentNumber;

    // Update session stats
    session.total++;
    if (correct) {
      session.correct++;
      session.streak++;
    } else {
      session.wrong++;
      session.streak = 0;
    }
    if (reactionTimeMs > 0) {
      session.reactionTimes.push(reactionTimeMs);
    }

    // Save to history
    Storage.addRecord({
      number: currentNumber,
      userAnswer: userAnswer,
      correct: correct,
      reactionTimeMs: reactionTimeMs,
    });

    // Show feedback
    showFeedback(correct, currentNumber, reactionTimeMs);

    // Update stats display
    updateStats();

    // Disable input while showing feedback
    els.answerInput.disabled = true;

    // Auto-advance
    const delay = (parseFloat(els.autoPlayDelay.value) || 1.5) * 1000;
    setTimeout(() => {
      playNumber();
    }, delay);
  }

  function showFeedback(correct, number, timeMs) {
    els.feedback.classList.remove('hidden', 'correct', 'wrong');

    if (correct) {
      els.feedback.classList.add('correct');
      els.feedback.textContent = '✅ Correct!';
    } else {
      els.feedback.classList.add('wrong');
      els.feedback.textContent = `❌ Wrong — the answer was ${number}`;
    }

    // Show reaction time
    if (timeMs > 0) {
      els.reactionTime.classList.remove('hidden');
      els.reactionTime.textContent = `⏱️ ${(timeMs / 1000).toFixed(2)}s`;
    }
  }

  function updateStats() {
    els.statTotal.textContent = session.total;
    els.statCorrect.textContent = session.correct;
    els.statWrong.textContent = session.wrong;
    els.statStreak.textContent = session.streak;

    if (session.total > 0) {
      els.statAccuracy.textContent =
        Math.round((session.correct / session.total) * 100) + '%';
    } else {
      els.statAccuracy.textContent = '0%';
    }

    if (session.reactionTimes.length > 0) {
      const avg =
        session.reactionTimes.reduce((a, b) => a + b, 0) /
        session.reactionTimes.length;
      els.statAvgTime.textContent = (avg / 1000).toFixed(1) + 's';
    } else {
      els.statAvgTime.textContent = '--';
    }
  }

  return { init };
})();

// Start the app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
