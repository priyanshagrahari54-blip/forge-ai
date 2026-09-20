/* Centralized browser text-to-speech for every Forge surface.
 *
 * Both the full cockpit (handsfree.js) and the lightweight home page
 * (forge-home.html) speak through this module, so voice output behaves
 * identically everywhere:
 *
 *  * voice selection follows the same language preference the recognizer
 *    uses (Hindi, Indian English);
 *  * Chrome's "voice list populates late" and "speak() right after
 *    cancel() is dropped" quirks are handled in one place;
 *  * prime() spends a user gesture so the browser's autoplay policy
 *    allows later replies (the "Enable Voice" action).
 *
 * Playback is browser-local: nothing here calls the control plane.
 */
"use strict";
(() => {
  const synth = window.speechSynthesis;
  const Utterance = window.SpeechSynthesisUtterance;
  const MAX_CHARS = 2000;
  const NAMED = /natural|neural|google|microsoft|zira|samantha/i;

  function supported() {
    return !!synth && typeof synth.speak === "function" && !!Utterance;
  }

  function voices() {
    try {
      return (synth && typeof synth.getVoices === "function" && synth.getVoices()) || [];
    } catch (_) {
      return [];
    }
  }

  function pickVoice(lang) {
    const wanted = String(lang || "").toLowerCase();
    const list = voices();
    if (!wanted) return list.find(v => NAMED.test(String(v.name || ""))) || null;
    return list.find(v => String(v.lang || "").toLowerCase() === wanted)
      || list.find(v => String(v.lang || "").toLowerCase().startsWith(wanted.slice(0, 2)))
      || list.find(v => NAMED.test(String(v.name || "")))
      || null;
  }

  function cancel() {
    if (synth && typeof synth.cancel === "function") synth.cancel();
  }

  /* Speak one reply. Resolves when playback ends and rejects with the real
     browser error when synthesis is refused, so callers can report it
     instead of pretending the reply was spoken. */
  function speak(text, options) {
    const opts = options || {};
    return new Promise((resolve, reject) => {
      if (!supported()) {
        reject(new Error("Browser text-to-speech is unavailable."));
        return;
      }
      const utterance = new Utterance(String(text == null ? "" : text).slice(0, MAX_CHARS));
      utterance.lang = opts.lang || "en-IN";
      utterance.rate = typeof opts.rate === "number" ? opts.rate : 0.96;
      utterance.pitch = typeof opts.pitch === "number" ? opts.pitch : 1.06;
      utterance.volume = typeof opts.volume === "number" ? opts.volume : 1.0;
      const voice = pickVoice(utterance.lang);
      if (voice) utterance.voice = voice;
      if (!voice && synth && typeof synth.addEventListener === "function") {
        // Chrome populates the voice list asynchronously.
        const refresh = () => {
          const late = pickVoice(utterance.lang);
          if (late) utterance.voice = late;
          if (synth.removeEventListener) synth.removeEventListener("voiceschanged", refresh);
        };
        synth.addEventListener("voiceschanged", refresh, { once: true });
      }
      const done = () => { if (opts.onEnd) opts.onEnd(); };
      utterance.onstart = () => { if (opts.onStart) opts.onStart(); };
      utterance.onend = () => { done(); resolve(true); };
      utterance.onerror = event => {
        done();
        reject(new Error("Browser speech synthesis failed: "
          + ((event && event.error) || "unknown error")));
      };
      cancel();
      try {
        synth.speak(utterance);
      } catch (err) {
        done();
        reject(err);
        return;
      }
      // Some Chromium builds need one event-loop turn after cancel().
      setTimeout(() => {
        try {
          if (!synth.speaking && !synth.pending) synth.speak(utterance);
        } catch (_) { /* the error handler above reports real failures */ }
      }, 120);
    });
  }

  /* Spend a user gesture on playback so later replies are permitted by the
     autoplay policy. Returns false when the browser has no speech output. */
  function prime() {
    if (!supported()) return false;
    try {
      const utterance = new Utterance("Voice enabled.");
      utterance.lang = "en-IN";
      utterance.rate = 1;
      utterance.volume = 1;
      cancel();
      synth.speak(utterance);
      return true;
    } catch (_) {
      return false;
    }
  }

  window.ForgeVoicePlayback = {
    supported, speak, cancel, prime, pickVoice, voices,
  };
})();
