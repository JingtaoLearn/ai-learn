// tts.js - Text-to-Speech using Web Speech API

const TTS = (() => {
  let selectedVoice = null;
  let isReady = false;

  function init() {
    return new Promise((resolve) => {
      const synth = window.speechSynthesis;

      function pickVoice() {
        const voices = synth.getVoices();
        // Prefer en-US voices, then any English voice
        selectedVoice =
          voices.find((v) => v.lang === 'en-US' && v.localService) ||
          voices.find((v) => v.lang === 'en-US') ||
          voices.find((v) => v.lang.startsWith('en-')) ||
          voices[0] ||
          null;
        isReady = true;
        resolve();
      }

      if (synth.getVoices().length > 0) {
        pickVoice();
      } else {
        synth.onvoiceschanged = () => {
          pickVoice();
        };
        // Fallback timeout in case onvoiceschanged never fires
        setTimeout(() => {
          if (!isReady) {
            pickVoice();
          }
        }, 1000);
      }
    });
  }

  function speak(number) {
    return new Promise((resolve, reject) => {
      const synth = window.speechSynthesis;
      // Cancel any ongoing speech
      synth.cancel();

      const utterance = new SpeechSynthesisUtterance(String(number));
      if (selectedVoice) {
        utterance.voice = selectedVoice;
      }
      utterance.lang = 'en-US';
      utterance.rate = 0.9;
      utterance.pitch = 1;

      utterance.onend = () => resolve();
      utterance.onerror = (e) => {
        // 'interrupted' and 'canceled' are expected when replaying
        if (e.error === 'interrupted' || e.error === 'canceled') {
          resolve();
        } else {
          reject(e);
        }
      };

      synth.speak(utterance);
    });
  }

  function cancel() {
    window.speechSynthesis.cancel();
  }

  return { init, speak, cancel };
})();
