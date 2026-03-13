// app.js - Main application logic

const App = (() => {
  // State
  let currentNumber = null;
  let isPlaying = false;
  let isRunning = false;
  let autoAdvanceTimer = null;
  let editLog = [];
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
    els.stopBtn = document.getElementById('stop-btn');
    els.replayBtn = document.getElementById('replay-btn');
    els.answerInput = document.getElementById('answer-input');
    els.feedback = document.getElementById('feedback');
    els.reactionTime = document.getElementById('reaction-time');
    els.overlay = document.getElementById('training-overlay');
    // Overlay stats
    els.overlayCorrect = document.getElementById('overlay-correct');
    els.overlayTotal = document.getElementById('overlay-total');
    els.overlayAccuracy = document.getElementById('overlay-accuracy');
    // Main page stats
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
    els.numpadBtns = document.querySelectorAll('.numpad-btn');
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
    // Play button — opens overlay and starts
    els.playBtn.addEventListener('click', startSession);

    // Stop button
    els.stopBtn.addEventListener('click', stopSession);

    // Replay button
    els.replayBtn.addEventListener('click', replayNumber);

    // Answer input — Enter to submit
    els.answerInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitAnswer();
      }
    });

    // Track keyboard edits on the answer input
    els.answerInput.addEventListener('input', () => {
      editLog.push({ action: 'input', value: els.answerInput.value, ts: Date.now() });
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' && e.target !== els.answerInput) return;
      if (e.target === els.answerInput && e.key !== ' ') return;

      if (e.key === ' ' && e.target !== els.answerInput) {
        e.preventDefault();
        if (isRunning) return;
        startSession();
      } else if ((e.key === 'r' || e.key === 'R') && e.target !== els.answerInput) {
        e.preventDefault();
        replayNumber();
      } else if (e.key === 'Escape') {
        stopSession();
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

    // Number pad
    els.numpadBtns.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const value = btn.dataset.value;
        const action = btn.dataset.action;
        if (value !== undefined) {
          if (!els.answerInput.disabled) {
            els.answerInput.value += value;
            editLog.push({ action: 'digit', value: els.answerInput.value, ts: Date.now() });
          }
        } else if (action === 'backspace') {
          if (!els.answerInput.disabled) {
            els.answerInput.value = els.answerInput.value.slice(0, -1);
            editLog.push({ action: 'backspace', value: els.answerInput.value, ts: Date.now() });
          }
        } else if (action === 'submit') {
          submitAnswer();
        }
      });
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

  function startSession() {
    // Reset session stats
    session = { total: 0, correct: 0, wrong: 0, streak: 0, reactionTimes: [] };
    // Show overlay
    els.overlay.classList.remove('hidden');
    // Prevent background scroll
    document.body.style.overflow = 'hidden';
    updateOverlayStats();
    // Clear feedback
    els.feedback.textContent = '\u00A0';
    els.feedback.className = 'feedback';
    els.reactionTime.textContent = '\u00A0';
    // Start first number
    playNumber();
  }

  async function playNumber() {
    if (isPlaying) return;
    isPlaying = true;
    isRunning = true;

    currentNumber = getRandomNumber();

    // Reset input
    els.answerInput.value = '';
    els.answerInput.disabled = false;
    els.replayBtn.disabled = false;

    // Clear feedback text but keep space
    els.feedback.textContent = '\u00A0';
    els.feedback.className = 'feedback';
    els.reactionTime.textContent = '\u00A0';

    Timer.reset();
    editLog = [];

    try {
      await TTS.speak(currentNumber);
      Timer.start();
    } catch (err) {
      console.error('TTS error:', err);
    }

    isPlaying = false;
  }

  function stopSession() {
    isRunning = false;
    isPlaying = false;
    currentNumber = null;

    if (autoAdvanceTimer) {
      clearTimeout(autoAdvanceTimer);
      autoAdvanceTimer = null;
    }

    TTS.cancel && TTS.cancel();
    window.speechSynthesis && window.speechSynthesis.cancel();
    Timer.reset();

    // Hide overlay
    els.overlay.classList.add('hidden');
    document.body.style.overflow = '';

    // Reset
    els.answerInput.value = '';
    els.answerInput.disabled = true;
    els.replayBtn.disabled = true;

    // Update main page stats
    updateStats();
  }

  async function replayNumber() {
    if (currentNumber === null || isPlaying) return;
    isPlaying = true;

    Timer.reset();
    editLog = [];

    try {
      await TTS.speak(currentNumber);
      Timer.start();
    } catch (err) {
      console.error('TTS replay error:', err);
    }

    isPlaying = false;
  }

  function submitAnswer() {
    if (currentNumber === null || isPlaying) return;

    const raw = els.answerInput.value.trim();
    if (raw === '') return;

    const userAnswer = parseInt(raw, 10);
    if (isNaN(userAnswer)) return;

    const reactionTimeMs = Timer.stop();
    const correct = userAnswer === currentNumber;

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

    Storage.addRecord({
      number: currentNumber,
      userAnswer: userAnswer,
      correct: correct,
      reactionTimeMs: reactionTimeMs,
      edits: editLog.slice(),
    });

    showFeedback(correct, currentNumber, reactionTimeMs);
    updateOverlayStats();
    updateStats();

    els.answerInput.disabled = true;

    if (isRunning) {
      const delay = (parseFloat(els.autoPlayDelay.value) || 1.5) * 1000;
      autoAdvanceTimer = setTimeout(() => {
        autoAdvanceTimer = null;
        if (isRunning) {
          playNumber();
        }
      }, delay);
    }
  }

  function showFeedback(correct, number, timeMs) {
    els.feedback.className = 'feedback';

    if (correct) {
      els.feedback.classList.add('correct');
      els.feedback.textContent = '✅ Correct';
    } else {
      els.feedback.classList.add('wrong');
      els.feedback.textContent = `❌ ${number}`;
    }

    if (timeMs > 0) {
      els.reactionTime.textContent = `⏱️ ${(timeMs / 1000).toFixed(2)}s`;
    }
  }

  function updateOverlayStats() {
    els.overlayCorrect.textContent = session.correct;
    els.overlayTotal.textContent = session.total;
    if (session.total > 0) {
      els.overlayAccuracy.textContent =
        Math.round((session.correct / session.total) * 100) + '%';
    } else {
      els.overlayAccuracy.textContent = '—';
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

document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
