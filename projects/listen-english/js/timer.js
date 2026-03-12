// timer.js - Reaction time measurement

const Timer = (() => {
  let startTime = null;

  function start() {
    startTime = performance.now();
  }

  function stop() {
    if (startTime === null) return 0;
    const elapsed = performance.now() - startTime;
    startTime = null;
    return Math.round(elapsed);
  }

  function reset() {
    startTime = null;
  }

  function isRunning() {
    return startTime !== null;
  }

  return { start, stop, reset, isRunning };
})();
