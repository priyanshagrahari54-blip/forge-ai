const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup() {
  const el = (tag, cls, text) => ({tag, cls, textContent:text, children:[], classList:{toggle(name,value){this[name]=value}}, append(...items){this.children.push(...items)}, appendChild(item){this.children.push(item)}, replaceChildren(){this.children=[]}});
  const nodes = Object.fromEntries(['network-status','network-connections','network-agents'].map(id=>[id,el('div')]));
  const ctx={el, state:{session:{project_id:'test-project'}}, document:{getElementById:id=>nodes[id]}, snapEpoch:()=>0, stale:()=>false, api:async()=>({agents:[]})};
  const code=fs.readFileSync('forge/cockpit/web/app.js','utf8');
  vm.runInNewContext(code.slice(code.indexOf('function renderNetworkHealth(')),ctx);
  return {ctx,nodes};
}
test('runtime graph does not invent healthy connections or active workers',()=>{
  const {ctx,nodes}=setup(); ctx.renderNetworkHealth({});
  assert.equal(nodes['network-status'].textContent,'Dispatcher unknown');
  assert.equal(nodes['network-status'].classList['is-live'],false);
  assert.match(nodes['network-connections'].children[1].children[1].children[1].textContent,/— healthy/);
});
test('runtime health uses backend counts and working route links',()=>{
  const {ctx,nodes}=setup(); ctx.renderNetworkHealth({workers:{running:true,busy:2,max_per_project:4},models:{healthy:3,unavailable:1},approvals_waiting:2});
  assert.equal(nodes['network-status'].textContent,'● Dispatcher live');
  assert.equal(nodes['network-connections'].children[2].href,'#/agents');
  assert.equal(nodes['network-connections'].children[2].children[1].children[1].textContent,'2 busy / 4 capacity');
});
test('catalog labels simulated agents and handles empty/error states',async()=>{
  const {ctx,nodes}=setup();
  await ctx.renderNetworkCatalog();
  assert.equal(nodes['network-agents'].children[0].textContent,'No agents registered.');
  ctx.api=async()=>({agents:[{name:'Planner',simulated:true}]});
  await ctx.renderNetworkCatalog();
  assert.equal(nodes['network-agents'].children[0].children[2].textContent,'Simulated');
  ctx.api=async()=>{throw new Error('offline')};
  await ctx.renderNetworkCatalog();
  assert.match(nodes['network-agents'].textContent,/unavailable/);
});
