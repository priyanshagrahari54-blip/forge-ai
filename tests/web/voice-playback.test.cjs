const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

/* One shared speaker for every Forge surface: both the cockpit
   (handsfree.js) and the lightweight home page (forge-home.html) must speak
   through voice-playback.js instead of talking to speechSynthesis directly,
   and the "Enable Voice" action must spend a user gesture on playback so the
   browser autoplay policy permits later replies. */
function setup(voices) {
  const spoken = [];
  class Utterance {
    constructor(text) { this.text = text; this.lang = ''; this.voice = null; }
  }
  const synth = {
    speaking: false, pending: false,
    getVoices: () => voices,
    addEventListener() {}, removeEventListener() {},
    cancel() { this.cancelled = (this.cancelled || 0) + 1; },
    speak(utterance) { spoken.push(utterance); },
  };
  const context = {console, Promise, setTimeout, clearTimeout,
    window: {speechSynthesis: synth, SpeechSynthesisUtterance: Utterance}};
  context.window.window = context.window;
  vm.runInNewContext(fs.readFileSync('forge/cockpit/web/voice-playback.js', 'utf8'), context);
  return {playback: context.window.ForgeVoicePlayback, spoken, synth};
}

test('shared playback picks the requested voice and resolves on end', async () => {
  const h = setup([{name: 'Hindi India', lang: 'hi-IN'}, {name: 'English India', lang: 'en-IN'}]);
  assert.equal(h.playback.supported(), true);
  let started = 0, ended = 0;
  const done = h.playback.speak('नमस्ते, मैं फ़ोर्ज़ हूँ', {
    lang: 'hi-IN', onStart: () => { started++; }, onEnd: () => { ended++; },
  });
  assert.equal(h.spoken.length, 1);
  assert.equal(h.spoken[0].lang, 'hi-IN');
  assert.equal(h.spoken[0].voice.lang, 'hi-IN');
  h.spoken[0].onstart();
  h.spoken[0].onend();
  assert.equal(await done, true);
  assert.equal(started, 1);
  assert.equal(ended, 1);
});

test('playback reports refusal instead of pretending the reply was spoken', async () => {
  const h = setup([]);
  const failed = h.playback.speak('hello', {lang: 'en-IN'});
  h.spoken[0].onerror({error: 'not-allowed'});
  await assert.rejects(failed, /not-allowed/);
});

test('prime() spends a user gesture and degrades honestly when unsupported', () => {
  const h = setup([]);
  assert.equal(h.playback.prime(), true);
  assert.equal(h.spoken.length, 1);
  const bare = {window: {}};
  vm.runInNewContext(fs.readFileSync('forge/cockpit/web/voice-playback.js', 'utf8'), bare);
  assert.equal(bare.window.ForgeVoicePlayback.supported(), false);
  assert.equal(bare.window.ForgeVoicePlayback.prime(), false);
  return assert.rejects(bare.window.ForgeVoicePlayback.speak('hi'), /unavailable/);
});

test('both Forge surfaces load and use the shared playback module', () => {
  const index = fs.readFileSync('forge/cockpit/web/index.html', 'utf8');
  const home = fs.readFileSync('forge/cockpit/web/forge-home.html', 'utf8');
  const handsfree = fs.readFileSync('forge/cockpit/web/handsfree.js', 'utf8');
  assert.ok(index.indexOf('/voice-playback.js') > index.indexOf('/app.js'));
  assert.ok(index.indexOf('/voice-playback.js') < index.indexOf('/handsfree.js'));
  assert.match(home, /src="\/voice-playback\.js"/);
  assert.match(home, /id="enable-voice"/);
  assert.match(home, /Playback\.speak\(/);
  assert.match(home, /JSON\.stringify\(\{text,confirm:true\}\)/);
  assert.ok(!/speechSynthesis\.speak\(/.test(handsfree), 'handsfree.js must not speak directly');
  assert.match(handsfree, /window\.ForgeVoicePlayback/);
  assert.match(handsfree, /body: \{text, confirm: true\}/);
});
