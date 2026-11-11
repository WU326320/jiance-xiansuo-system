const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Run the real application functions without starting login or external requests.
const source = fs.readFileSync(path.join(__dirname, '../workbench/web/app.js'), 'utf8').split('(async function init()')[0];
function harness() {
  const nodes = new Map();
  const element = () => ({ innerHTML: '', textContent: '', value: '', checked: false, disabled: false,
    dataset: {}, classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {}, removeAttribute() {},
    focus() {}, reportValidity() { return true; }, checkValidity() { return true; }, appendChild() {}, remove() {},
    querySelectorAll() { return []; } });
  const context = vm.createContext({ console, URL, URLSearchParams, FormData, AbortController, Date,
    setTimeout: () => 1, clearTimeout() {}, location: { hash: '#leads', href: '', origin: 'http://test' },
    history: { replaceState() {}, pushState() {} }, window: { addEventListener() {} },
    document: { querySelector(s) { if (!nodes.has(s)) nodes.set(s, element()); return nodes.get(s); },
      querySelectorAll() { return []; }, createElement: element, addEventListener() {}, activeElement: null,
      body: { classList: { add() {}, remove() {} } } },
    fetch: async () => { throw new Error('unexpected request'); } });
  vm.runInContext(source, context);
  vm.runInContext("state.me = {id: 1, username: 'reviewer', role: 'supervisor'}", context);
  return { context, nodes, run: (code) => vm.runInContext(code, context) };
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const leads = (no) => ({ items: [{ lead_no: no, shop_name: no, status: '待复核', ip_codes: [] }], total: 1, page: 1, page_size: 20, stats: { tiers: {} } });

test('only the latest lead query may update the list', async () => {
  const h = harness(), old = deferred(), latest = deferred(); let n = 0;
  h.context.api = () => (++n === 1 ? old.promise : latest.promise);
  const first = h.run('loadLeads()'), second = h.run('loadLeads()');
  latest.resolve(leads('LATEST')); await second;
  old.resolve(leads('OLD')); await first;
  assert.equal(h.run('state.leads.items[0].lead_no'), 'LATEST');
});
test('lead response after navigating away is ignored', async () => {
  const h = harness(), request = deferred(); h.context.api = () => request.promise;
  const pending = h.run('loadLeads()'); h.run("state.view = 'tasks'; state.viewEpoch++");
  request.resolve(leads('OLD')); await pending;
  assert.equal(h.run('state.leads.items.length'), 0);
});
test('busy action rejects double click and unlocks after failure', async () => {
  const h = harness(), pending = deferred(); let calls = 0;
  h.context.work = () => { calls++; return pending.promise; };
  const first = h.run("withBusy($('#submit-task'), '提交中…', work)");
  await h.run("withBusy($('#submit-task'), '提交中…', work)");
  assert.equal(calls, 1); assert.equal(h.nodes.get('#submit-task').disabled, true);
  pending.resolve(); await first;
  assert.equal(h.nodes.get('#submit-task').disabled, false);
  h.context.work = async () => { throw new Error('network'); };
  await assert.rejects(h.run("withBusy($('#submit-task'), '提交中…', work)"), /network/);
  assert.equal(h.nodes.get('#submit-task').disabled, false);
});
test('reversed date interval is rejected before fetching', async () => {
  const h = harness(); let calls = 0; h.context.api = async () => { calls++; return leads('X'); };
  h.run("state.filters.date_from='2026-11-07'; state.filters.date_to='2026-11-06'");
  await h.run('loadLeads()'); assert.equal(calls, 0);
});
test('selection header distinguishes partial and full current-page selection', () => {
  const h = harness(); h.run("state.leads.items=[{lead_no:'A',status:'待复核'},{lead_no:'B',status:'待复核'},{lead_no:'C',status:'已录入SaaS'}]; state.selected.add('A'); renderBatchBar()");
  assert.equal(h.nodes.get('#check-all').indeterminate, true);
  h.run("state.selected.add('B'); renderBatchBar()");
  assert.equal(h.nodes.get('#check-all').checked, true);
  assert.equal(h.nodes.get('#check-all').indeterminate, false);
});
test('unsafe product URLs cannot become executable links', () => {
  const h = harness();
  assert.equal(h.run("safeUrl('javascript:alert(1)')"), '');
  assert.equal(h.run("safeUrl('data:text/html,bad')"), '');
  assert.equal(h.run("safeUrl('https://item.example/a?x=1')"), 'https://item.example/a?x=1');
});
test('task refresh keeps the current form draft', async () => {
  const h = harness(); h.run("state.view='tasks'; $('#t-ip').value='未提交的 IP'");
  h.context.api = async () => ({ items: [] });
  await h.run('refreshTasks()');
  assert.equal(h.nodes.get('#t-ip').value, '未提交的 IP');
});
test('whitelist preview is not applied if the IP changed while waiting', async () => {
  const h = harness(), request = deferred(); h.context.api = () => request.promise;
  h.run("state.view='tasks'; $('#t-ip').value='IP-A'");
  const pending = h.run('previewIp()'); h.run("$('#t-ip').value='IP-B'");
  request.resolve({ ip: { id: 1, name: 'IP-A', company_code: 'a' }, whitelist: [] }); await pending;
  assert.equal(h.nodes.get('#t-company')?.value || '', '');
  assert.equal(h.run('state.whitelistLoaded'), false);
});

test('closing the task form invalidates an outstanding IP preview', async () => {
  const h = harness(), request = deferred(); h.context.api = () => request.promise;
  h.run("state.view='tasks'; $('#t-ip').value='IP-A'");
  const pending = h.run('previewIp()');
  // Closing the real dialog advances previewRequest, even without changing views.
  h.run('state.previewRequest++');
  request.resolve({ ip: { id: 1, name: 'IP-A', company_code: 'stale' }, whitelist: [] });
  await pending;
  assert.equal(h.run('state.whitelistLoaded'), false);
  assert.equal(h.nodes.get('#t-company')?.value || '', '');
});

test('slow management responses cannot overwrite the next page', async () => {
  for (const [view, call, response] of [
    ['ips', 'renderIps()', {items: []}],
    ['accounts', 'renderAccounts()', {items: []}],
    ['settings', 'renderSettings()', {settings: {}, smtp_status: {}}],
    ['update', 'renderUpdate()', {current: {}, history: []}],
  ]) {
    const h = harness(), request = deferred(); h.context.api = () => request.promise;
    h.run(`state.view='${view}'; $('#main').innerHTML='current'`);
    const pending = h.run(call);
    h.run("state.view='leads'; state.viewEpoch++; $('#main').innerHTML='new-page'");
    request.resolve(response); await pending;
    assert.equal(h.nodes.get('#main').innerHTML, 'new-page', view);
  }
});

test('task creation rejects concurrent calls and restores retry after failure', async () => {
  const h = harness(), request = deferred(); let calls = 0;
  h.context.toast = () => {};
  h.context.saveTaskDraft = () => {};
  h.context.api = () => { calls++; return request.promise; };
  h.run("state.view='tasks'; state.taskDraft={values:{'t-ip':'saved'}}");
  const first = h.run("executeTask({ip_name:'saved'})");
  await h.run("executeTask({ip_name:'saved'})");
  assert.equal(calls, 1);
  request.resolve(Promise.reject(new Error('offline'))); await first;
  assert.equal(h.run('state.taskSubmitting'), false);
  assert.equal(h.nodes.get('#task-fields').disabled, false);
  assert.equal(h.nodes.get('#confirm-run').disabled, false);
  assert.equal(h.run("state.taskDraft.values['t-ip']"), 'saved');
});
