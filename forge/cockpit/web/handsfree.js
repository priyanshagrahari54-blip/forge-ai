/* Browser speech is opt-in; all execution remains in the authorized control plane. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  let enabled = false, busy = false, speaking = false, recognition = null;
  let conversation = null, owner = null, generation = 0, retry = null;
  let tasks = new Map(), polling = false;
  const status = text => { $("hf-status").textContent = text; };
  function log(who, text) {
    const p = document.createElement("p");
    const label = document.createElement("strong");
    label.textContent = who + "  ";
    p.append(label, document.createTextNode(text));
    $("hf-log").append(p);
    while ($("hf-log").children.length > 40) $("hf-log").firstChild.remove();
    $("hf-log").scrollTop = $("hf-log").scrollHeight;
  }
  function pause() {
    clearTimeout(retry);
    if (recognition) { const r = recognition; recognition = null; r.abort(); }
    $("hf-orb").classList.remove("listening");
  }
  function stop() {
    enabled = false;
    pause();
    // Playback lives in voice-playback.js so every Forge surface speaks
    // through one implementation.
    if (window.ForgeVoicePlayback) window.ForgeVoicePlayback.cancel();
    else if (window.speechSynthesis) window.speechSynthesis.cancel();
    speaking = false;
    $("hf-toggle").textContent = "Enable hands-free";
    $("hf-toggle").setAttribute("aria-pressed", "false");
    status("Microphone off · Tasks already submitted are not cancelled");
  }
  function recognitionLanguage() {
    const nav = (typeof navigator !== "undefined" && navigator) || window.navigator || {};
    const languages = (nav.languages || []).map(v => String(v).toLowerCase());
    const browser = String(nav.language || "").toLowerCase();
    if (languages.some(v => v.startsWith("hi")) || browser.startsWith("hi")) return "hi-IN";
    if (languages.some(v => v === "en-in")) return "en-IN";
    return "en-IN";
  }
  function transcriptScore(text) {
    const value = String(text || "").trim().toLowerCase();
    if (!value) return -1;
    let score = 0;
    if (/^[0-9+\-*/%.() ×÷ ]+$/.test(value)) score += 100;
    if (/\b(plus|minus|times|multiplied|divided|over|calculate|compute|what is|what's)\b/.test(value)) score += 50;
    if (/\b(run tests|commit|review|summarize|check status|update the website)\b/.test(value)) score += 40;
    return score;
  }
  function listen() {
    if (!enabled || busy || speaking || recognition || document.hidden) return;
    if (!Speech) { status("Browser speech is unavailable · Type a command instead"); return; }
    const r = new Speech();
    recognition = r;
    r.lang = recognitionLanguage();
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 3;
    r.onstart = () => {
      if (recognition !== r) return;
      status("Listening · Speak a command");
      $("hf-orb").classList.add("listening");
    };
    r.onresult = event => {
      if (recognition !== r || busy || speaking) return;
      let final = "", interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          let best = result[0] ? result[0].transcript : "";
          let bestScore = transcriptScore(best);
          for (let j = 1; j < result.length; j++) {
            const candidate = result[j].transcript;
            const score = transcriptScore(candidate);
            if (score > bestScore) { best = candidate; bestScore = score; }
          }
          final += best + " ";
        } else interim += result[0] ? result[0].transcript : "";
      }
      $("hf-interim").textContent = interim;
      if (final.trim()) submit(final.trim());
    };
    r.onerror = event => {
      if (recognition !== r) return;
      if (event.error !== "no-speech" && event.error !== "aborted") {
        stop();
        status("Speech unavailable: " + event.error + ". Check microphone permissions or type below.");
      }
    };
    r.onend = () => {
      if (recognition !== r) return;
      recognition = null;
      $("hf-orb").classList.remove("listening");
      if (enabled) retry = setTimeout(listen, 700);
    };
    try { r.start(); } catch (_) { stop(); status("Could not start microphone. Try again or type a command."); }
  }
  function speakReply(text) {
    const playback = window.ForgeVoicePlayback;
    if (!playback || !playback.supported()) {
      throw new Error("Browser text-to-speech is unavailable.");
    }
    return playback.speak(text, {
      lang: recognitionLanguage(),
      onStart: () => { speaking = true; status("Speaking · Microphone paused"); },
      onEnd: () => { speaking = false; },
    });
  }

  async function reply(text) {
    log("Forge", text);
    const sound = $("hf-sound");
    if (!enabled || !sound || !sound.checked) {
      if (enabled) status("Reply received · Spoken replies are off");
      listen();
      return;
    }
    pause();
    try {
      await speakReply(text);
    } catch (err) {
      status(err.message + " · Reply is visible in the transcript");
      log("System", err.message);
    } finally {
      speaking = false;
      if (enabled) listen();
    }
  }

  async function submit(text) {
    syncSession();
    if (/^stop listening[.!?]?$/i.test(text)) { stop(); return; }
    if (busy) return;
    if (!state.session) { status("Sign in to a project first."); return; }
    busy = true;
    pause();
    const version = generation;
    $("hf-interim").textContent = "";
    status("Processing · Microphone paused");
    log("You", text);
    let message;
    try {
      if (!conversation) {
        const created = await api("/api/v1/voice/conversations", {method: "POST"});
        if (version !== generation) return;
        conversation = created.conversation_id;
        log("System", created.simulation ? "Backend voice intent stack: simulation. Browser transcription is real." : "Backend voice stack connected.");
      }
      // Confirmation is contextual and decided server-side: informational
      // turns answer immediately, routine actions execute directly under the
      // permission policy, and consequential ones (commits, messages, calls)
      // ask "Shall I …?" and only act on the next affirmative utterance.
      const result = await api(`/api/v1/voice/conversations/${encodeURIComponent(conversation)}/say`,
        {method: "POST", body: {text, confirm: false}});
      if (version !== generation) return;
      $("hf-api").textContent = "Control plane · connected /api/v1";
      message = result.spoken || "No spoken result returned.";
      if (result.task && result.task.kind === "task" && result.task.task_id) {
        tasks.set(result.task.task_id, "");
        refreshTasks();
      }
    } catch (err) {
      if (version !== generation) return;
      $("hf-api").textContent = "Control plane · request failed";
      message = "Command could not be confirmed: " + err.message + ". Check Tasks before retrying.";
      if (err.status === 404) conversation = null;
    } finally {
      if (version === generation) {
        busy = false;
        if (message) reply(message);
        if (!enabled) status("Microphone off · Reply available below");
      }
    }
  }
  async function refreshTasks() {
    if (polling || !state.session || !tasks.size) return;
    polling = true;
    const version = generation;
    try {
      for (const [id, previous] of tasks) {
        const data = await api(`/api/v1/tasks/${encodeURIComponent(id)}`);
        if (version !== generation) return;
        const task = data.task;
        const summary = `${task.status} · ${task.stage || "waiting for agent"}`;
        if (summary !== previous) {
          tasks.set(id, summary);
          const row = document.createElement("p");
          const link = document.createElement("a");
          link.href = "#/tasks/" + encodeURIComponent(id);
          link.textContent = id.slice(0, 12) + " ↗";
          row.append(link, document.createTextNode(" — " + summary));
          $("hf-activity").prepend(row);
          while ($("hf-activity").children.length > 20) $("hf-activity").lastChild.remove();
          if (TERMINAL.includes(task.status)) {
            tasks.delete(id);
            reply(`Task ${id.slice(0, 8)} ${task.status.toLowerCase().replaceAll("_", " ")}. ${task.requirement || ""} Open the task report for verified changes and results.`);
          }
        }
      }
    } catch (err) { $("hf-api").textContent = "Task updates unavailable · " + err.message; }
    finally { polling = false; }
  }
  $("hf-browser").textContent = Speech ? "Browser speech · available (Indian English/Hindi)" : "Browser speech · unsupported; type instead";
  $("hf-toggle").disabled = !Speech || !window.isSecureContext;
  if (!window.isSecureContext) status("Microphone requires HTTPS or localhost. Text commands are available.");
  $("hf-toggle").onclick = () => {
    syncSession();
    if (enabled) return stop();
    enabled = true;
    // Spend the click on speech output: browsers refuse speech synthesis
    // that was not authorized by a user gesture (autoplay policy).
    if (window.ForgeVoicePlayback && window.ForgeVoicePlayback.supported()) {
      window.ForgeVoicePlayback.prime();
    }
    $("hf-toggle").textContent = "Stop listening";
    $("hf-toggle").setAttribute("aria-pressed", "true");
    listen();
  };
  $("hf-form").onsubmit = event => {
    event.preventDefault();
    const text = $("hf-text").value.trim();
    if (text && !busy) { $("hf-text").value = ""; submit(text); }
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { pause(); if (enabled) status("Listening paused · Tab is hidden"); }
    else listen();
  });
  window.addEventListener("pagehide", stop);
  function syncSession() {
    const session = state.session;
    const key = session ? JSON.stringify([session.id, session.session_id, session.project_id, session.actor]) : null;
    if (owner !== key) {
      stop(); generation++; busy = false; conversation = null; tasks.clear(); owner = key;
      $("hf-log").replaceChildren();
      $("hf-activity").textContent = "No voice task yet.";
      $("hf-api").textContent = "Control plane · not checked";
    }
    $("hf-project").textContent = session ? "Project · " + session.project_id : "Project · no session";
  }
  setInterval(() => { syncSession(); refreshTasks(); }, 2500);
})();
