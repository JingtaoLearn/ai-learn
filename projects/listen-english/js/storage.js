// storage.js - localStorage persistence for settings and answer history

const Storage = (() => {
  const KEYS = {
    SETTINGS: 'listen-english-settings',
    HISTORY: 'listen-english-history',
  };

  const DEFAULT_SETTINGS = {
    min: 1,
    max: 100,
    autoPlayDelay: 1.5,
  };

  function getSettings() {
    try {
      const saved = localStorage.getItem(KEYS.SETTINGS);
      if (saved) {
        return { ...DEFAULT_SETTINGS, ...JSON.parse(saved) };
      }
    } catch (e) {
      // ignore parse errors
    }
    return { ...DEFAULT_SETTINGS };
  }

  function saveSettings(settings) {
    localStorage.setItem(KEYS.SETTINGS, JSON.stringify(settings));
  }

  function getHistory() {
    try {
      const saved = localStorage.getItem(KEYS.HISTORY);
      if (saved) {
        return JSON.parse(saved);
      }
    } catch (e) {
      // ignore parse errors
    }
    return [];
  }

  function addRecord(record) {
    const history = getHistory();
    history.push({
      number: record.number,
      userAnswer: record.userAnswer,
      correct: record.correct,
      reactionTimeMs: record.reactionTimeMs,
      timestamp: Date.now(),
    });
    localStorage.setItem(KEYS.HISTORY, JSON.stringify(history));
  }

  function clearHistory() {
    localStorage.removeItem(KEYS.HISTORY);
  }

  return {
    getSettings,
    saveSettings,
    getHistory,
    addRecord,
    clearHistory,
  };
})();
