const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup() {
  const nodes = new Map();
  const node = () => ({textContent:'', value:'', checked:false, children:[], classList:{add(){},remove(){}}, setAttribute(){}, append(...items){this.children.push(...items)}, prepend(item){this.children.unshift(item)}, replaceChildren(){this.children=[]}});
  const get = id => {if (!nodes.has(id)) nodes.set(id,node()); return nodes.get(id)};
  let recognizer, tick;
  class Speech {constructor(){recognizer=this} start(){this.onstart()} abort(){this.onend()}}
  const calls=[];
  const context = {console, Map, JSON, setTimeout, clearTimeout, setInterval(fn){tick=fn},
    document:{getElementById:get, documentElement:{lang:'en'}, hidden:false, createElement:node, createTextNode:t=>t, addEventListener(){}},
    window:{SpeechRecognition:Speech,isSecureContext:true,addEventListener(){}},
    state:{session:{id:'s',project_id:'demo',actor:'tester'}}, TERMINAL:['SUCCEEDED','FAILED'],
    api:async (url, opts)=>{calls.push([url,opts]); return url.endsWith('/say') ? {spoken:'Shall I review?'} : {conversation_id:'c',simulation:true}}
  };
  vm.runInNewContext(fs.readFileSync('forge/cockpit/web/handsfree.js','utf8'),context);
  return {get,context,calls,tick,recognizer:()=>recognizer};
}
test('opt-in, auto-restart lifecycle and stop command never reaches API', async()=>{
  const h=setup();
  assert.equal(h.recognizer(),undefined);
  h.get('hf-toggle').onclick();
  assert.match(h.get('hf-status').textContent,/Listening/);
  h.get('hf-text').value='stop listening';
  h.get('hf-form').onsubmit({preventDefault(){}});
  assert.match(h.get('hf-status').textContent,/Microphone off/);
  assert.equal(h.calls.length,0);
});
test('typed command uses conversation endpoint and leaves confirmation to the contextual server policy', async()=>{
  const h=setup();
  h.get('hf-text').value='review the code';
  h.get('hf-form').onsubmit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.calls.length,2);
  assert.equal(h.calls[1][0],'/api/v1/voice/conversations/c/say');
  assert.equal(h.calls[1][1].body.confirm,false);
  assert.equal(h.calls[1][1].body.text,'review the code');
});
test('permission errors stop microphone instead of retrying indefinitely',()=>{
  const h=setup(); h.get('hf-toggle').onclick();
  h.recognizer().onerror({error:'not-allowed'});
  assert.match(h.get('hf-status').textContent,/not-allowed/);
  assert.equal(h.get('hf-toggle').textContent,'Enable hands-free');
});
test('session switch stops recognition and clears conversation display',()=>{
  const h=setup(); h.get('hf-toggle').onclick();
  h.context.state.session=null; h.tick();
  assert.equal(h.get('hf-toggle').textContent,'Enable hands-free');
  assert.equal(h.get('hf-log').children.length,0);
});
