// Run with node test_panel.cjs; no browser or credentials required.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('claude_meter.py', 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', hidden: false, disabled: false,
    classList: { add() {}, remove() {} },
    addEventListener(event, handler) { this[event] = handler; },
    focus() {},
  });
  return elements.get(id);
}
const context = {
  document: { querySelector: element, querySelectorAll: () => [] },
  fetch: async (path) => ({ json: async () => {
    if (path === '/api/oauth/start') return {ok: true, flow_id: 'test', url: 'https://claude.ai/'};
    if (path === '/api/oauth/complete') return {ok: false, error: 'Claude refused the connection (HTTP 400). Start again.'};
    throw new Error('state unavailable in test');
  }}),
  setTimeout: () => 1, clearTimeout() {}, setInterval() {},
};
vm.createContext(context);
vm.runInContext(script, context);
(async () => {
  await element('#startOauth').click();
  element('#oauthCode').value = 'test-code';
  await element('#finishOauth').click();
  assert.match(element('#oauthResult').textContent, /HTTP 400/);
  assert.equal(element('#oauthResult').hidden, false);
  assert.equal(element('#finishOauth').disabled, false);
  assert.equal(element('#oauthCode').value, '');
  console.log('Panel keeps the connection error visible: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
