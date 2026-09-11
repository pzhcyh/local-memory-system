/* Focused UI regressions using the real app.js and an explicit minimal DOM/fetch
 * fixture. No browser, server, model endpoint, keychain or user Vault is used.
 * Run: node --test tests/test_ui_reconnect.cjs
 */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const VAULT_ID = 'b90a0570-b7f3-41a0-a597-e03a04cc8e12';
const VAULT_PATH = '/synthetic/vault';
const PROJECT = '重连合成项目';

class Element {
  constructor(tag = 'div') {
    Object.assign(this, {tag, value: '', checked: false, disabled: false, hidden: false,
      readOnly: false, dataset: {}, files: [], children: [], listeners: new Map(), _text: ''});
    this.classList = {toggle() {}};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  get firstElementChild() { return this.children[0]; }
  get selectedOptions() { return this.children.filter(child => child.value === this.value); }
  addEventListener(type, listener) { this.listeners.set(type, [...(this.listeners.get(type) || []), listener]); }
  emit(type) { return Promise.all((this.listeners.get(type) || []).map(listener => listener({preventDefault() {}}))); }
  querySelectorAll() { return []; }
  setAttribute() {}
  focus() { this.focused = true; }
  select() { this.selected = true; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  scrollIntoView() {}
}

function overview() {
  return {project: PROJECT, sourceSignature: 'synthetic-signature', entryPath: '/synthetic/CURRENT.md',
    current: {version: 4, status: 'current', entries: [{entryId: 'entry-4', text: '原有合成摘录', quote: '原有合成摘录',
      version: 1, source: {id: 'synthetic-source'}, classification: 'source-statement'}]},
    versions: [{version: 4, status: 'current', reason: 'synthetic'}]};
}

function harness() {
  const elements = new Map([...htmlSource.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const timers = new Map(), requests = [];
  let timerId = 0;
  let fetchHandler = async () => { throw new TypeError('Explicit synthetic offline fixture'); };
  const context = {
    document: {getElementById: id => elements.get(id), createElement: tag => new Element(tag), querySelectorAll: () => [],
      querySelector: () => ({value: 'text'})},
    window: {addEventListener() {}, location: {pathname: '/', search: ''}, history: {replaceState() {}}},
    navigator: {}, crypto, TextEncoder, URLSearchParams, AbortController, DOMException, TypeError,
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, {callback, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (url, options) => { requests.push({url, options}); return fetchHandler(url, options); },
  };
  vm.createContext(context);
  // Skip only automatic startup; test the unchanged request/action/render code.
  vm.runInContext(appSource.replace(/\nbootstrap\(\);\s*$/, '\n') + '\nglobalThis.ui = {state, workState, modelState, navigationState, connectionState, request, reconnectService, refreshKnowledge, renderKnowledge, runWorkAction, contextState, prepareContext, saveContext, invalidateContext, refreshContexts, pathCopyControls, openNavigation, parseNavigationQuery, renderCsvTable, loadRecord, evidenceState, prepareEvidence, saveEvidence, refreshEvidenceList, invalidateEvidence, showPage, verificationState, verifyEvidence, refreshSteward};', context);
  const ui = context.ui;
  Object.assign(ui.state, {token: 'old-synthetic-token', vaultId: 'test', vaults: [{id: 'test', vaultId: VAULT_ID, path: VAULT_PATH, name: '测试库'}],
    status: {projects: [PROJECT]}, generation: 1});
  Object.assign(ui.workState, {project: PROJECT, knowledge: overview(), knowledgeLoad: 'ready', status: null});
  ui.connectionState.phase = 'ready';
  elements.get('vault-select').value = 'test';
  const target = {entryId: 'entry-4', title: '摘录 1', version: 4, eventId: 'synthetic-correction-event'};
  ui.workState.correction = target;
  elements.get('correction-text').value = '正确的合成内容';
  return {ui, elements, requests, timers, target, context,
    setFetch(handler) { fetchHandler = handler; },
    fireTimeout(ms) { const timer = [...timers.values()].find(item => item.ms === ms); assert.ok(timer, `Expected ${ms} ms timeout`); timer.callback(); }};
}

const response = (value, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => value});
function readyFetch(vaultId = VAULT_ID) {
  return async url => {
    if (url === '/api/bootstrap') return response({csrfToken: 'new-synthetic-token', vaults: [{id: 'test', vaultId, path: VAULT_PATH, name: '测试库'}]});
    if (url === '/api/vaults/test/status') return response({name: '测试库', path: VAULT_PATH, projects: [PROJECT]});
    if (url.startsWith('/api/vaults/test/records')) return response({records: []});
    if (url === '/api/models') return response({profiles: [], credentialStore: {available: false}});
    if (url.startsWith('/api/vaults/test/knowledge?')) return response(overview());
    if (url === '/api/vaults/test/steward') return response({running: false, paused: false, round: null, jobs: []});
    throw new Error('Unexpected fixture request: ' + url);
  };
}

async function settleWork(h) {
  for (let i = 0; i < 40 && h.ui.workState.busy; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.ui.workState.busy, false, 'UI action should finish');
}

test('acknowledged correction stays saved after invalidation and a failed follow-up read', async () => {
  for (const followUpFails of [false, true]) {
    const h = harness();
    const pending = overview();
    pending.current.status = 'pending-review';
    h.setFetch(async (url, options) => {
      if (url === '/api/vaults/test/corrections' && options.method === 'POST') return response({duplicate: false, current: pending});
      if (url.startsWith('/api/vaults/test/knowledge?')) {
        if (followUpFails) throw new TypeError('Synthetic read failure after committed write');
        return response(pending);
      }
      throw new Error('Unexpected request: ' + url);
    });
    h.elements.get('correction-form').emit('submit');
    await settleWork(h);
    h.ui.renderKnowledge();
    assert.equal(h.target.done, true);
    assert.equal(h.target.uncertain, false);
    assert.equal(h.elements.get('correction-submit').textContent, '本次纠正已保存');
    assert.equal(h.elements.get('correction-submit').disabled, true);
    assert.equal(h.elements.get('correction-text').readOnly, true);
    assert.match(h.elements.get('correction-guard').textContent, /已保存.*无需重复提交/);
    assert.doesNotMatch(h.elements.get('correction-guard').textContent, /目标已过期/);
    assert.equal(h.elements.get('correction-next').hidden, false);
    assert.equal(h.elements.get('correction-next').disabled, followUpFails);
    assert.equal(h.requests.filter(r => r.options.method === 'POST').length, 1);
  }
});

test('reloaded pending knowledge offers processing without inventing a saved form receipt', () => {
  const h = harness();
  h.ui.workState.correction = null;
  h.ui.workState.knowledge.current.status = 'pending-review';
  h.ui.renderKnowledge();
  assert.equal(h.elements.get('correction-next').hidden, false);
  assert.match(h.elements.get('correction-guard').textContent, /当前知识处于待复核/);
  assert.doesNotMatch(h.elements.get('correction-guard').textContent, /本次纠正已保存/);
});

test('offline correction retains original draft/event and final rendering stays failed', async () => {
  const h = harness();
  h.elements.get('correction-form').emit('submit');
  await settleWork(h);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.method, 'POST');
  assert.equal(h.ui.workState.correction, h.target);
  assert.equal(h.target.eventId, 'synthetic-correction-event');
  assert.equal(h.target.uncertain, true);
  assert.equal(h.target.payload.text, '正确的合成内容');
  assert.equal(h.elements.get('correction-text').value, '正确的合成内容');
  assert.equal(h.ui.workState.knowledgeLoad, 'failed');
  h.ui.renderKnowledge();
  assert.match(h.elements.get('knowledge-status').textContent, /读取知识失败/);
  assert.doesNotMatch(h.elements.get('knowledge-status').textContent, /正在读取|尚无加工知识/);
  assert.match(h.elements.get('knowledge-version-title').textContent, /上次读取.*当前状态未核实/);
  assert.equal(h.elements.get('correction-submit').disabled, true);
});

test('failure with no cached knowledge never becomes an empty-Vault conclusion', async () => {
  const h = harness();
  h.ui.workState.knowledge = null;
  await assert.rejects(h.ui.refreshKnowledge(), /重新连接/);
  h.ui.renderKnowledge();
  assert.match(h.elements.get('knowledge-entries').textContent, /无法判断是否存在加工知识/);
  assert.match(h.elements.get('knowledge-versions').textContent, /版本列表尚未取得/);
  assert.doesNotMatch(h.elements.get('knowledge-status').textContent, /正在读取|尚无加工知识/);
});

test('same-UUID reconnect rotates token, preserves all drafts and never replays POST', async () => {
  const h = harness();
  Object.assign(h.target, {uncertain: true, payload: {eventId: h.target.eventId, text: '原事件原正文'}});
  const frozen = JSON.stringify(h.target.payload);
  h.ui.connectionState.phase = 'offline';
  h.ui.workState.knowledgeLoad = 'failed';
  h.ui.workState.statusFingerprint = JSON.stringify({running: false, paused: false, round: null, jobs: []});
  h.elements.get('steward-status').textContent = '连接不可用，当前运行状态未知';
  h.ui.modelState.dirty = true;
  h.ui.navigationState.importDirty = true;
  h.elements.get('model-name').value = '尚未保存的连接名称';
  h.elements.get('import-content').value = '尚未提交的导入草稿';
  h.setFetch(readyFetch());
  await h.ui.reconnectService();
  assert.equal(h.ui.state.token, 'new-synthetic-token');
  assert.equal(h.ui.connectionState.phase, 'ready');
  assert.equal(h.ui.workState.knowledgeLoad, 'ready');
  assert.match(h.elements.get('steward-status').textContent, /当前没有运行中的轮次/);
  assert.equal(h.ui.workState.correction, h.target);
  assert.equal(h.target.uncertain, true);
  assert.equal(JSON.stringify(h.target.payload), frozen);
  assert.equal(h.elements.get('correction-text').value, '正确的合成内容');
  assert.equal(h.elements.get('model-name').value, '尚未保存的连接名称');
  assert.equal(h.elements.get('import-content').value, '尚未提交的导入草稿');
  assert.equal(h.ui.modelState.dirty, true);
  assert.ok(h.requests.length >= 3);
  assert.ok(h.requests.every(item => !item.options.method || item.options.method === 'GET'));
  assert.ok(h.requests.slice(1).every(item => item.options.headers['X-Memory-Token'] === 'new-synthetic-token'));
});

test('reconnect rejects changed UUID, copied directory or removed selected Vault before any write', async () => {
  for (const vaults of [[{id: 'test', vaultId: 'different-vault-uuid', path: VAULT_PATH}],
    [{id: 'test', vaultId: VAULT_ID, path: '/synthetic/copied-vault'}], []]) {
    const h = harness();
    h.ui.connectionState.phase = 'offline';
    h.setFetch(async () => response({csrfToken: 'other-service-token', vaults}));
    await h.ui.reconnectService();
    assert.equal(h.ui.connectionState.phase, 'identity-mismatch');
    assert.equal(h.ui.state.token, null);
    assert.equal(h.ui.state.vaults[0].vaultId, VAULT_ID);
    assert.equal(h.elements.get('correction-text').value, '正确的合成内容');
    await assert.rejects(h.ui.request('/api/vaults/test/corrections', {method: 'POST', body: '{}'}), /身份已变化/);
    assert.equal(h.requests.length, 1);
    assert.match(h.elements.get('notice').textContent, /重新加载/);
  }
});

test('initial connection failure can restore selectable Vaults and original navigation', async () => {
  const h = harness();
  Object.assign(h.ui.state, {vaultId: null, status: null, vaults: []});
  Object.assign(h.ui.workState, {project: '', correction: null, knowledge: null, knowledgeLoad: 'idle'});
  h.elements.get('correction-text').value = '';
  h.elements.get('vault-select').disabled = true;
  h.ui.connectionState.phase = 'offline';
  h.context.window.location.search = '?vault=test&page=knowledge&project=' + encodeURIComponent(PROJECT);
  h.setFetch(readyFetch());
  await h.ui.reconnectService();
  assert.equal(h.elements.get('vault-select').disabled, false);
  assert.ok(h.elements.get('vault-select').children.some(option => option.value === 'test'));
  assert.equal(h.ui.state.vaultId, 'test');
  assert.equal(h.ui.state.page, 'knowledge');
  assert.equal(h.ui.workState.project, PROJECT);
  assert.equal(h.ui.connectionState.phase, 'ready');
  assert.ok(h.requests.every(item => !item.options.method || item.options.method === 'GET'));
});

test('initial reconnect restores options without navigating over an unbound draft', async () => {
  const h = harness();
  Object.assign(h.ui.state, {vaultId: null, status: null, vaults: []});
  Object.assign(h.ui.workState, {project: '', correction: null, knowledge: null, knowledgeLoad: 'idle'});
  h.ui.connectionState.phase = 'offline';
  h.setFetch(readyFetch());
  await h.ui.reconnectService();
  assert.equal(h.elements.get('vault-select').disabled, false);
  assert.equal(h.ui.state.vaultId, null);
  assert.equal(h.elements.get('correction-text').value, '正确的合成内容');
  assert.equal(h.requests.length, 1);
});

test('TOKEN_REQUIRED points to reconnect, without automatic bootstrap or POST retry', async () => {
  const h = harness();
  h.setFetch(async () => response({error: {code: 'TOKEN_REQUIRED', message: 'synthetic expired session'}}, 403));
  await assert.rejects(h.ui.request('/api/vaults/test/knowledge?project=x'), error => error.code === 'TOKEN_REQUIRED' && /重新连接/.test(error.message));
  assert.equal(h.ui.connectionState.phase, 'session-expired');
  await assert.rejects(h.ui.request('/api/vaults/test/corrections', {method: 'POST', body: '{}'}), /重新连接/);
  assert.equal(h.requests.length, 1);
});

test('ordinary requests have a 20-second deadline including response body reads', async () => {
  const h = harness();
  h.setFetch(async (_url, options) => ({ok: true, status: 200, json: () => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('Synthetic timeout', 'AbortError')));
  })}));
  const pending = h.ui.request('/api/vaults/test/knowledge?project=x');
  await new Promise(resolve => setImmediate(resolve));
  h.fireTimeout(20000);
  await assert.rejects(pending, error => error.code === 'REQUEST_TIMEOUT');
  assert.equal(h.ui.connectionState.phase, 'offline');
  assert.equal(h.timers.size, 0);
});

test('model capability check gets independent 75-second tolerance for the 60-second backend check', async () => {
  const h = harness();
  h.setFetch(async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('Synthetic timeout', 'AbortError')));
  }));
  const pending = h.ui.request('/api/models/synthetic/check', {method: 'POST', body: '{}'});
  assert.equal(h.elements.get('reconnect-button').disabled, true);
  assert.equal([...h.timers.values()][0].ms, 75000);
  h.fireTimeout(75000);
  await assert.rejects(pending, error => error.code === 'REQUEST_TIMEOUT');
  assert.equal(h.ui.connectionState.pendingWrites, 0);
});

test('a late old-connection failure cannot poison the reconnected session', async () => {
  const h = harness();
  let rejectOld;
  h.setFetch(async () => new Promise((_resolve, reject) => { rejectOld = reject; }));
  const old = h.ui.request('/api/vaults/test/knowledge?project=old');
  h.setFetch(readyFetch());
  await h.ui.reconnectService();
  rejectOld(new TypeError('Late synthetic failure'));
  await assert.rejects(old);
  assert.equal(h.ui.connectionState.phase, 'ready');
  assert.equal(h.ui.state.token, 'new-synthetic-token');
  assert.equal(h.ui.workState.knowledgeLoad, 'ready');
});

test('reconnect does not race an in-flight POST or publish a token after scope generation changes', async () => {
  const h = harness();
  let releaseWrite;
  h.setFetch(async () => new Promise(resolve => { releaseWrite = resolve; }));
  const pendingWrite = h.ui.request('/api/vaults/test/corrections', {method: 'POST', body: '{}'});
  await h.ui.reconnectService();
  assert.equal(h.requests.length, 1);
  releaseWrite(response({synthetic: true}));
  await pendingWrite;
  let releaseBootstrap;
  h.setFetch(async () => new Promise(resolve => { releaseBootstrap = resolve; }));
  const reconnecting = h.ui.reconnectService();
  h.ui.state.generation += 1;
  releaseBootstrap(response({csrfToken: 'must-not-publish', vaults: [{id: 'test', vaultId: VAULT_ID}]}));
  await reconnecting;
  assert.equal(h.ui.state.token, null);
  assert.notEqual(h.ui.connectionState.phase, 'ready');
});

function contextFixture(h) {
  h.elements.get('search-project').value = PROJECT;
  h.elements.get('context-query').value = '会议 时间';
  h.elements.get('context-budget').value = '8192';
  return {schema: 1, project: PROJECT, query: '会议 时间', maxBytes: 8192, sourceSignature: 'fixture', knowledgeVersion: 5,
    knowledgeStatus: 'current', results: [{quote: '<img src=x onerror=alert(1)>周一10:30', score: 2, matchedTerms: ['会议', '时间'], sourceVersion: 1, knowledgeVersion: 5}], totalMatches: 2, omittedCount: 1, warnings: [], markdown: '# 合成引用', bytes: 100, snapshotId: 'fixture-snapshot'};
}

test('context ignores late preview after query or budget edits and never renders materials as HTML', async () => {
  const h = harness(), preview = contextFixture(h);
  let resolve;
  h.setFetch(() => new Promise(r => { resolve = r; }));
  const loading = h.ui.prepareContext();
  h.elements.get('context-query').value = 'changed';
  h.elements.get('context-query').emit('input');
  resolve(response(preview)); await loading;
  assert.equal(h.ui.contextState.preview, null);
  assert.equal(h.elements.get('context-save').disabled, true);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  assert.match(h.elements.get('context-results').textContent, /<img src=x/);
  assert.match(h.elements.get('context-warnings').textContent, /未纳入 1 条/);
  h.elements.get('context-budget').value = '2048'; h.elements.get('context-budget').emit('change');
  assert.equal(h.ui.contextState.preview, null);
});

test('context uncertain write survives reconnect without auto POST and explicitly retries exact event payload', async () => {
  const h = harness(), preview = contextFixture(h);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  h.setFetch(async () => { throw new TypeError('lost response'); }); await h.ui.saveContext();
  const body = JSON.stringify(h.ui.contextState.pending.body);
  assert.equal(h.requests.filter(r => r.options.method === 'POST').length, 1);
  assert.equal(h.elements.get('search-project').disabled, true);
  h.setFetch(readyFetch()); await h.ui.reconnectService();
  assert.equal(JSON.stringify(h.ui.contextState.pending.body), body);
  assert.equal(h.requests.filter(r => r.options.method === 'POST').length, 1);
  h.setFetch(async (url, options) => {
    if (options.method === 'POST') { assert.equal(options.body, body); return response({duplicate: true, path: '/synthetic/context.md', stale: true}); }
    throw new TypeError('list unavailable after committed save');
  });
  await h.ui.saveContext();
  assert.equal(h.ui.contextState.pending, null);
  assert.equal(h.elements.get('context-receipt').hidden, false);
  assert.equal(h.elements.get('context-saved-path').textContent, '/synthetic/context.md');
  assert.match(h.elements.get('context-saved-status').textContent, /已确认此前保存.*历史快照/);
  assert.match(h.elements.get('context-list').textContent, /读取失败/);
});

test('CONTEXT_STALE rejects old snapshot and requires new preview, with no automatic retry', async () => {
  const h = harness(), preview = contextFixture(h);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  h.setFetch(async () => response({error: {code: 'CONTEXT_STALE', message: 'fixture stale'}}, 409));
  await h.ui.saveContext();
  assert.equal(h.ui.contextState.pending, null); assert.equal(h.ui.contextState.preview, null);
  assert.match(h.elements.get('context-status').textContent, /旧快照不能保存/);
  await h.ui.saveContext();
  assert.equal(h.requests.filter(r => r.options.method === 'POST').length, 1);
});

test('knowledge refresh invalidates prepared context; saved files list is scoped and marks stale snapshots', async () => {
  const h = harness(), preview = contextFixture(h);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  h.setFetch(readyFetch()); await h.ui.refreshKnowledge();
  assert.equal(h.ui.contextState.preview, null);
  h.setFetch(async () => response({contexts: [{query: '会议', createdAt: 'fixture-date', knowledgeVersion: 4, stale: true, path: '/synthetic/old/context.md'}]}));
  await h.ui.refreshContexts();
  assert.match(h.elements.get('context-list').textContent, /已失效.*当前知识已变化/);
  assert.match(h.elements.get('context-list').textContent, /\/synthetic\/old\/context.md/);
});

test('pending context save prevents Vault switch, and late list cannot cross project scope', async () => {
  const h = harness(), preview = contextFixture(h);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  let resolve;
  h.setFetch(() => new Promise(r => { resolve = r; }));
  const saving = h.ui.saveContext();
  h.elements.get('vault-select').value = 'other'; h.elements.get('vault-select').emit('change');
  assert.equal(h.ui.state.vaultId, 'test'); assert.equal(h.elements.get('vault-select').value, 'test');
  h.setFetch(async () => response({contexts: []}));
  resolve(response({path: '/synthetic/context.md', duplicate: false, stale: false})); await saving;
  h.setFetch(() => new Promise(r => { resolve = r; }));
  const listing = h.ui.refreshContexts();
  h.elements.get('search-project').value = 'other-project';
  resolve(response({contexts: [{query: 'must not render', path: '/wrong/context.md'}]})); await listing;
  assert.doesNotMatch(h.elements.get('context-list').textContent, /must not render|wrong/);
});

test('changing project clears previous saved receipt and copy target; citation body is not repeated', async () => {
  const h = harness(), preview = contextFixture(h);
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  const resultCard = h.elements.get('context-results').children[0];
  assert.equal(resultCard.textContent.split(preview.results[0].quote).length - 1, 1);
  h.setFetch(async (url, options) => options.method === 'POST'
    ? response({path: '/synthetic/project-a/context.md', duplicate: false, stale: false})
    : response({contexts: []}));
  await h.ui.saveContext();
  assert.equal(h.elements.get('context-receipt').hidden, false);
  h.elements.get('search-project').value = 'another-project';
  h.elements.get('search-project').emit('change');
  assert.equal(h.ui.contextState.receipt, null);
  assert.equal(h.elements.get('context-receipt').hidden, true);
  assert.equal(h.elements.get('context-saved-path').textContent, '');
  assert.equal(h.elements.get('context-save').disabled, true);
});


test('context copy verifies exact browser clipboard after write resolves', async () => {
  const h = harness(); let resolve, actual;
  h.context.navigator.clipboard = {writeText(value) { actual = value; return new Promise(r => {resolve = r;}); }, readText: async () => actual};
  const [copy, manual, field, status] = h.ui.pathCopyControls('/合成/上下文.md').children;
  const running = copy.emit('click');
  assert.equal(actual, '/合成/上下文.md'); assert.equal(copy.disabled, true);
  assert.doesNotMatch(status.textContent, /复制成功/);
  resolve(); await running;
  assert.match(status.textContent, /浏览器剪贴板回读与完整路径一致/); assert.equal(copy.disabled, false);
  assert.equal(field.hidden, true);
});

test('denied Clipboard API uses selected full path and legacy copy only reports a true result', async () => {
  const h = harness(); let called = 0;
  h.context.navigator.clipboard = {writeText: async () => {throw new DOMException('denied', 'NotAllowedError');}};
  const [copy, manual, field, status] = h.ui.pathCopyControls('/合成/完整 路径.md').children;
  h.context.document.execCommand = command => {
    called++; assert.equal(command, 'copy'); assert.equal(field.hidden, false);
    assert.equal(field.value, '/合成/完整 路径.md'); assert.equal(field.focused, true);
    assert.equal(field.selectionEnd, field.value.length); return true;
  };
  await copy.emit('click'); assert.equal(called, 1);
  assert.match(status.textContent, /自动复制未确认/);
});

test('unavailable or rejected clipboard and failed fallback keep manual selection without false success', async () => {
  for (const behavior of ['missing', 'denied', 'throws']) {
    const h = harness();
    if (behavior !== 'missing') h.context.navigator.clipboard = {writeText: async () => {throw new DOMException('denied', 'NotAllowedError');}};
    h.context.document.execCommand = () => {if (behavior === 'throws') throw new Error('unsupported'); return false;};
    const [copy, manual, field, status] = h.ui.pathCopyControls('/合成/不可自动复制.md').children;
    await copy.emit('click');
    assert.match(status.textContent, /自动复制未确认/); assert.doesNotMatch(status.textContent, /复制成功|已通过备用/);
    assert.equal(field.hidden, false); assert.equal(field.readOnly, true);
    assert.equal(field.selectionStart, 0); assert.equal(field.selectionEnd, field.value.length);
    await manual.emit('click'); assert.match(status.textContent, /完整路径已选中/);
    assert.equal(copy.disabled, false);
  }
});

test('receipt and saved-list copy controls use their own exact paths and manual action makes no API call', async () => {
  const h = harness(), preview = contextFixture(h), writes = [];
  h.context.navigator.clipboard = {writeText: async value => {writes.push(value);}};
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  h.setFetch(async (url, options) => options.method === 'POST'
    ? response({path: '/合成/本次/context.md', duplicate: false, stale: false})
    : response({contexts: [{path:'/合成/历史/context.md', query:'历史', knowledgeVersion:4, stale:true}]}));
  await h.ui.saveContext();
  const receipt = h.elements.get('context-copy-tools').children[0];
  const card = h.elements.get('context-list').children[0];
  const history = card.children[card.children.length - 1];
  await receipt.children[0].emit('click'); await history.children[0].emit('click');
  assert.deepEqual(writes, ['/合成/本次/context.md','/合成/历史/context.md']);
  await history.children[1].emit('click'); assert.equal(writes.length, 2);
  assert.equal(history.children[2].value, '/合成/历史/context.md');
  h.elements.get('search-project').value='另一个项目'; await h.elements.get('search-project').emit('change');
  assert.equal(h.elements.get('context-copy-tools').children.length, 0);
});


test('resolved write with empty, wrong, missing or rejected readback cannot claim success', async () => {
  for (const read of [async () => '', async () => '/wrong', undefined, async () => {throw new Error('denied');}]) {
    const h = harness();
    h.context.navigator.clipboard = {writeText: async () => {}, readText: read};
    h.context.document.execCommand = () => true;
    const [copy, , field, status] = h.ui.pathCopyControls('/expected/context.md').children;
    await copy.emit('click');
    assert.match(status.textContent, /自动复制未确认/);
    assert.doesNotMatch(status.textContent, /回读与完整路径一致|复制成功/);
    assert.equal(field.hidden, false);
    assert.equal(field.selectionEnd, field.value.length);
    assert.equal(copy.disabled, false);
  }
});

test('empty clipboard after resolved write is repaired only by verified fallback', async () => {
  const h = harness(); let clipboard = '';
  h.context.navigator.clipboard = {writeText: async () => {}, readText: async () => clipboard};
  h.context.document.execCommand = () => {clipboard = '/expected/context.md'; return true;};
  const [copy, , , status] = h.ui.pathCopyControls('/expected/context.md').children;
  await copy.emit('click');
  assert.match(status.textContent, /备用复制后，浏览器剪贴板回读与完整路径一致/);
});


test('fresh search deep links do not depend on unconfigured steward or model services', async () => {
  for (const project of ['', PROJECT]) {
    const h = harness();
    h.ui.state.vaultId = null; h.ui.state.status = null;
    h.elements.get('correction-text').value = '';
    h.setFetch(async url => {
      if (url.endsWith('/status')) return response({projects:[PROJECT], name:'测试库'});
      if (url.includes('/records')) return response({records:[]});
      if (url.includes('/contexts')) return response({contexts:[]});
      return response({error:{code:'STEWARD_UNCONFIGURED', message:'not configured'}}, 400);
    });
    await h.ui.openNavigation({vault:'test',page:'search',project}, {initial:true});
    assert.equal(h.ui.state.page, 'search');
    assert.equal(h.elements.get('search-project').value, project);
    assert.equal(h.elements.get('page-search').hidden, false);
    assert.ok(!h.requests.some(r => /steward|models|knowledge/.test(r.url)));
  }
});

test('context diagnostics explain budget exclusion without claiming raw material is absent', async () => {
  const h = harness(), preview = contextFixture(h);
  preview.diagnostics = {scopeProject: PROJECT, availableEntries:3, evaluatedEntries:3,
    matchedEntries:2, includedEntries:1, budgetExcludedEntries:1, noLexicalMatchEntries:1,
    statusExcludedEntries:0, exclusionReason:null, notice:'未命中不代表原文没有答案。'};
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  const text = h.elements.get('context-warnings').textContent;
  assert.match(text, /词法检查 3，匹配 2，纳入 1，预算排除 1，无词法交集 1/);
  assert.match(text, /未命中不代表原文没有答案/);
});

test('saved list renders each stale reason and still supports older missing diagnostics', async () => {
  const h = harness(); contextFixture(h);
  h.setFetch(async () => response({contexts:[{path:'/synthetic/context.md',query:'会议',knowledgeVersion:1,
    stale:true,staleReasons:['source-changed','knowledge-version-changed','knowledge-status-changed']}]}));
  await h.ui.refreshContexts();
  assert.match(h.elements.get('context-list').textContent, /来源已变化；知识版本已变化；知识状态已变化/);
  h.setFetch(async () => response(contextFixture(h))); await h.ui.prepareContext();
  assert.match(h.elements.get('context-warnings').textContent, /该快照未提供诊断字段/);
});


test('all matches excluded by budget are not called no lexical matches', async () => {
  const h = harness(), preview = contextFixture(h);
  preview.results = []; preview.totalMatches = 1; preview.omittedCount = 1;
  h.setFetch(async () => response(preview)); await h.ui.prepareContext();
  assert.match(h.elements.get('context-warnings').textContent, /有词法匹配，但预算内未纳入引用/);
  assert.doesNotMatch(h.elements.get('context-warnings').textContent, /当前加工知识无匹配/);
});


function descendants(element) { return [element, ...element.children.flatMap(descendants)]; }
function csvResult() {
  return {record:{id:'csv',version:1,filename:'sample.csv',project:PROJECT,path:'originals/test/v1/content/sample.csv',status:'latest-source-version',parseStatus:'unsupported',source:{}},
    table:{parserVersion:'csv-utf8-v1',sourceSHA:'sha',sourceVersion:1,parseStatus:'parsed',columnCount:2,dataRecordCount:51,
      rawText:'名,名\r\n甲,"一\r\n二"\r\n',header:{recordNumber:1,lineStart:1,lineEnd:1,cells:['名','名']},
      rows:Array.from({length:51},(_,i)=>({recordNumber:i+2,lineStart:2,lineEnd:3,cells:['<script>alert(1)</script>', '=1+1']}))}};
}

test('CSV preview pages at fifty and renders values as text with record/column source coordinates', async () => {
  const h=harness(), box=new Element(), result=csvResult();
  h.ui.renderCsvTable(box,result);
  assert.equal(descendants(box).filter(e=>e.tag==='td').length,100);
  assert.ok(descendants(box).some(e=>e.tag==='button' && e.textContent==='<script>alert(1)</script>'));
  assert.ok(!descendants(box).some(e=>e.tag==='script'));
  await descendants(box).find(e=>e.tag==='button' && e.textContent==='=1+1').emit('click');
  assert.match(box.textContent,/逻辑记录 2（含表头） · 第 2 列 · 原文 L2–L3/);
  assert.ok(descendants(box).some(e=>e.tag==='pre' && e.textContent==='甲,"一\r\n二"\r'));
  await descendants(box).find(e=>e.tag==='button' && e.textContent==='下一页').emit('click');
  assert.equal(descendants(box).filter(e=>e.tag==='td').length,2);
  assert.match(box.textContent,/第 2 \/ 2 页/);
});

test('CSV failure has no partial table and late table response cannot pollute next record', async () => {
  const h=harness(), box=new Element();
  const failed=csvResult(); failed.table={parserVersion:'v1',sourceSHA:'sha',sourceVersion:1,parseStatus:'failed',error:{code:'invalid-csv',message:'invalid'}};
  h.ui.renderCsvTable(box,failed);
  assert.match(box.textContent,/原文已保存，表格未解析/);
  assert.equal(descendants(box).filter(e=>e.tag==='table').length,0);
  let resolve, started;
  const pending=new Promise(r=>{started=r;});
  h.setFetch(async url=>{
    if(url.includes('/table?')) {started(); return new Promise(r=>{resolve=r;});}
    if(url.includes('/csv/')) return response({record:csvResult().record,content:null});
    return response({record:{...csvResult().record,id:'txt',filename:'other.txt'},content:'OTHER TEXT'});
  });
  const first=h.ui.loadRecord('csv',1); await pending;
  await h.ui.loadRecord('txt',1); resolve(response(csvResult())); await first;
  assert.match(h.elements.get('record-detail').textContent,/OTHER TEXT/);
  assert.doesNotMatch(h.elements.get('record-detail').textContent,/CSV 结构视图已解析/);
});

function evidenceFixture(overrides = {}) {
  const row = {recordId: 'csv-source', sourceVersion: 2, sourceSHA: 'sha', parserVersion: 'csv-utf8-v1', path: 'originals/csv/v2/content/a.csv', recordNumber: 2, lineStart: 2, lineEnd: 3, cells: ['=1+1', '<img src=x>', 'line\r\nnext'], matchedColumns: [2], rawExcerpt: '=1+1,<img src=x>,"line\r\nnext"\r\n'};
  return {status: 'complete', counts: {scopeSourceCount: 1, scopeSourceBytes: 100, scannedSourceCount: 1, failedSourceCount: 0, matchedRecordCount: 1, returnedRecordCount: 1, returnLimitExcludedCount: 0}, results: [row], selectedRecords: [row], includedRecordCount: 1, budgetExcludedCount: 0, bytes: 1200, maxBytes: 8192, markdown: '# CSV original evidence', snapshotId: 'snapshot', saveAllowed: true, notice: '原始来源证据，未经知识加工/事实确认', errors: [], ...overrides};
}
function evidenceHarness() {
  const h = harness(); h.ui.workState.correction = null; h.elements.get('correction-text').value = '';
  h.elements.get('search-project').value = PROJECT;
  h.elements.get('evidence-query').value = ' <img ';
  h.elements.get('evidence-budget').value = '8192';
  return h;
}
test('CSV evidence preserves query spaces and separates decoded text from exact raw record and explicit version', async () => {
  const h = evidenceHarness(), fixture = evidenceFixture();
  h.setFetch(async (url) => {
    if (url.includes('/csv-evidence')) { assert.equal(new URLSearchParams(url.split('?')[1]).get('q'), ' <img '); return response(fixture); }
    if (url.includes('/records/csv-source/versions/2')) return response({record: {recordId: 'csv-source', version: 2, filename: 'a.csv', project: PROJECT, source: {}, parseStatus: 'unsupported', contentPath: fixture.results[0].path}, content: null});
    throw Error(url);
  });
  await h.ui.prepareEvidence();
  assert.match(h.elements.get('evidence-results').textContent, /解码值/);
  assert.match(h.elements.get('evidence-results').textContent, /原始CSV片段/);
  assert.ok(h.elements.get('evidence-results').textContent.includes(fixture.results[0].rawExcerpt));
  assert.equal(h.ui.contextState.preview, null);
  const card = h.elements.get('evidence-results').children[0];
  await card.children.at(-1).emit('click');
  assert.ok(h.requests.some(r => r.url.includes('/records/csv-source/versions/2')));
});
test('CSV blocked and partial previews distinguish unknown counts, failure and separate exclusions', async () => {
  const h = evidenceHarness();
  h.setFetch(async () => response(evidenceFixture({status: 'blocked', saveAllowed: false, results: [], selectedRecords: [], counts: {scopeSourceCount: 33, scopeSourceBytes: 100, scannedSourceCount: 0, failedSourceCount: 0, matchedRecordCount: null, returnedRecordCount: 0, returnLimitExcludedCount: null}, errors: [{code: 'scan-limit-exceeded', limit: 'sourceCount'}]})));
  await h.ui.prepareEvidence();
  assert.equal(h.elements.get('evidence-save').disabled, true);
  assert.match(h.elements.get('evidence-status').textContent, /匹配数未知/);
  const calls = h.requests.length; await h.ui.saveEvidence(); assert.equal(h.requests.length, calls);
  h.setFetch(async () => response(evidenceFixture({status: 'partial', budgetExcludedCount: 1, errors: [{recordId: 'bad', sourceVersion: 3, code: 'invalid-csv'}]})));
  await h.ui.prepareEvidence();
  assert.match(h.elements.get('evidence-status').textContent, /计数仅覆盖成功部分/);
  assert.match(h.elements.get('evidence-status').textContent, /返回上限排除 0.*预算排除 1/);
  assert.match(h.elements.get('evidence-warnings').textContent, /invalid-csv.*v3/);
  assert.equal(h.elements.get('evidence-save').disabled, false);
});
test('late CSV preview and list cannot cross query, project or page changes', async () => {
  const h = evidenceHarness(); let resolve;
  h.setFetch(() => new Promise(r => { resolve = r; }));
  const pending = h.ui.prepareEvidence();
  h.elements.get('evidence-query').value = 'new'; await h.elements.get('evidence-query').emit('input');
  resolve(response(evidenceFixture())); await pending;
  assert.equal(h.ui.evidenceState.preview, null);
  const list = h.ui.refreshEvidenceList();
  h.elements.get('search-project').value = 'other';
  resolve(response({packages: [{query: 'WRONG PROJECT'}]})); await list;
  assert.ok(!h.elements.get('evidence-list').textContent.includes('WRONG PROJECT'));
  h.setFetch(async () => response(evidenceFixture())); await h.ui.prepareEvidence();
  h.ui.state.page = 'search'; h.ui.showPage('records', {refresh: false});
  assert.equal(h.ui.evidenceState.preview, null);
});
test('uncertain CSV save preserves original event and blocks scope navigation until explicit retry', async () => {
  const h = evidenceHarness(); h.setFetch(async () => response(evidenceFixture())); await h.ui.prepareEvidence();
  h.setFetch(async () => { throw new TypeError('offline'); }); await h.ui.saveEvidence();
  const body = JSON.stringify(h.ui.evidenceState.pending.body);
  assert.equal(h.elements.get('search-project').disabled, true);
  assert.equal(h.elements.get('evidence-query').disabled, true);
  await assert.rejects(h.ui.openNavigation({vault: 'test', page: 'records', project: PROJECT}), /未完成/);
  const writes = h.requests.filter(r => r.options.method === 'POST').length;
  h.setFetch(readyFetch()); await h.ui.reconnectService();
  assert.equal(h.requests.filter(r => r.options.method === 'POST').length, writes);
  assert.equal(JSON.stringify(h.ui.evidenceState.pending.body), body);
  h.setFetch(async (url, options) => {
    if (options.method === 'POST') { assert.equal(options.body, body); return response({duplicate: true, path: '/synthetic/evidence.md', stale: true, staleReasons: ['source-scope-changed']}); }
    return response({packages: []});
  });
  await h.ui.saveEvidence();
  assert.equal(h.ui.evidenceState.pending, null);
  assert.equal(h.elements.get('evidence-saved-path').textContent, '/synthetic/evidence.md');
  assert.match(h.elements.get('evidence-saved-status').textContent, /已确认此前保存.*来源范围已变化/);
  assert.equal(h.elements.get('evidence-receipt').hidden, false);
});
test('CSV stale rejection clears old preview and does not automatically refresh or write again', async () => {
  const h = evidenceHarness(); h.setFetch(async () => response(evidenceFixture())); await h.ui.prepareEvidence();
  h.setFetch(async () => response({error: {code: 'EVIDENCE_STALE', message: 'changed'}}, 409));
  await h.ui.saveEvidence();
  assert.equal(h.ui.evidenceState.pending, null); assert.equal(h.ui.evidenceState.preview, null);
  assert.equal(h.requests.filter(r => r.options.method === 'POST').length, 1);
  assert.match(h.elements.get('evidence-status').textContent, /旧快照不能保存/);
});
test('initial CSV search deep link loads saved packages and renders stale reason and reusable path control', async () => {
  const h = evidenceHarness(); h.ui.state.vaultId = null; h.ui.state.status = null;
  h.setFetch(async url => {
    if (url.endsWith('/status')) return response({projects: [PROJECT], name: 'test'});
    if (url.includes('/records')) return response({records: []});
    if (url.includes('/contexts')) return response({contexts: []});
    if (url.includes('/csv-evidence')) return response({packages: [{query: 'saved-csv-query', createdAt: 'today', status: 'partial', includedRecordCount: 1, bytes: 1234, stale: true, staleReasons: ['parser-version-changed'], path: '/csv/evidence.md'}]});
    throw Error(url);
  });
  await h.ui.openNavigation({vault: 'test', page: 'search', project: PROJECT}, {initial: true});
  assert.match(h.elements.get('evidence-list').textContent, /saved-csv-query.*解析器版本已变化/s);
  assert.match(h.elements.get('evidence-list').textContent, /复制文件路径/);
  assert.ok(h.requests.some(r => r.url.includes('/csv-evidence?project=')));
});
test('uncertain CSV save retains original event through retry auth and integrity errors, then reconnect confirms exact request', async () => {
  for (const [code, status] of [['TOKEN_REQUIRED', 403], ['SOURCE_INTEGRITY', 409]]) {
    const h = evidenceHarness(); h.setFetch(async () => response(evidenceFixture())); await h.ui.prepareEvidence();
    h.setFetch(async () => { throw new TypeError('lost response after possible commit'); });
    await h.ui.saveEvidence();
    const body = JSON.stringify(h.ui.evidenceState.pending.body);
    h.setFetch(readyFetch()); await h.ui.reconnectService();
    h.setFetch(async (url, options) => { assert.equal(options.body, body); return response({error: {code, message: 'cannot check prior event'}}, status); });
    await h.ui.saveEvidence();
    assert.equal(JSON.stringify(h.ui.evidenceState.pending.body), body);
    assert.equal(h.elements.get('search-project').disabled, true);
    assert.match(h.elements.get('evidence-status').textContent, /尚未确认/);
    const writes = h.requests.filter(r => r.options.method === 'POST').length;
    h.setFetch(readyFetch()); await h.ui.reconnectService();
    assert.equal(h.requests.filter(r => r.options.method === 'POST').length, writes);
    h.setFetch(async (url, options) => {
      if (options.method === 'POST') { assert.equal(options.body, body); return response({duplicate: true, path: '/synthetic/confirmed-evidence.md', stale: false, staleReasons: []}); }
      return response({packages: []});
    });
    await h.ui.saveEvidence();
    assert.equal(h.ui.evidenceState.pending, null);
    assert.match(h.elements.get('evidence-saved-status').textContent, /已确认此前保存/);
  }
});
function verificationFixture(overrides = {}) {
  return {schema: 1, kind: 'csv-source-evidence-verification', project: PROJECT, packageId: 'a'.repeat(64), currentReuseStatus: 'usable', packageStatus: 'partial', reasonCode: 'verified', stale: false, staleReasons: [], path: 'knowledge/source-evidence/a/evidence.md', snapshotId: 'saved-snapshot', sourceSignature: 'saved-source', currentSourceSignature: 'saved-source', markdownSHA: 'sha', markdown: '# 原包\r\n=1+1,<script>\r\n', notice: '原始来源证据，未经知识加工/事实确认。核验只代表检查时刻。保留部分扫描限制。', packageCounts: {scopeSourceCount: 2, scopeSourceBytes: 100, scannedSourceCount: 2, failedSourceCount: 1, scannedRecordCount: 3, matchedRecordCount: 2, returnedRecordCount: 2, returnLimitExcludedCount: 0, savedIncludedRecordCount: 1, savedBudgetExcludedCount: 1}, ledger: {recordBytes: 2000, markdownBytes: 1000, currentScopeSourceCount: 2, currentScopeSourceBytes: 100, currentSourceBytesRead: 100, currentSourceFilesRead: 2, uniqueFilesRead: 4, logicalReferencesChecked: 1, uniqueReferencedSourceFiles: 1, duplicateReferenceCount: 0, measuredReadBytes: 3100, unmeasuredInternalIO: '不包含 Vault 内部扫描'}, ...overrides};
}
test('M4F usable partial package shows original limits and exact Markdown separately from current verification', async () => {
  const h = evidenceHarness(), report = verificationFixture();
  h.setFetch(async url => { const p = new URLSearchParams(url.split('?')[1]); assert.equal(p.get('packageId'), report.packageId); assert.equal(p.get('includeMarkdown'), '1'); return response(report); });
  await h.ui.verifyEvidence(report.packageId);
  assert.match(h.elements.get('verification-status').textContent, /usable.*核验通过/);
  assert.match(h.elements.get('verification-report').textContent, /原包扫描状态：partial/);
  assert.match(h.elements.get('verification-report').textContent, /预算排除 1/);
  assert.match(h.elements.get('verification-report').textContent, /显式读取字节合计3100/);
  assert.match(h.elements.get('verification-report').textContent, /不是总 I\/O、token/);
  assert.equal(h.elements.get('verification-body').value, report.markdown);
  assert.equal(h.elements.get('verification-body-label').hidden, false);
  assert.ok(!h.requests.some(r => r.options.method === 'POST'));
});
test('M4F stale parser and algorithm reasons suppress Markdown, HTTP409 blocked renders complete ledger with unknown checks', async () => {
  const h = evidenceHarness();
  h.setFetch(async () => response(verificationFixture({currentReuseStatus: 'stale', stale: true, staleReasons: ['parser-version-changed', 'algorithm-changed']})));
  await h.ui.verifyEvidence('a'.repeat(64));
  assert.match(h.elements.get('verification-report').textContent, /解析器版本已变化.*检索算法已变化/);
  assert.equal(h.elements.get('verification-body').value, '');
  assert.equal(h.elements.get('verification-body-label').hidden, true);
  const report = verificationFixture({currentReuseStatus: 'blocked', stale: null, staleReasons: [], currentSourceSignature: null});
  Object.assign(report.ledger, {currentScopeSourceCount: 33, currentSourceFilesRead: 0, currentSourceBytesRead: 0, logicalReferencesChecked: null, uniqueReferencedSourceFiles: null, duplicateReferenceCount: null});
  h.setFetch(async () => response(report, 409));
  await h.ui.verifyEvidence('a'.repeat(64));
  assert.match(h.elements.get('verification-status').textContent, /blocked.*尚未完成核验/);
  assert.match(h.elements.get('verification-report').textContent, /是否过期未知/);
  assert.match(h.elements.get('verification-report').textContent, /当前源正文已读字节0/);
  assert.match(h.elements.get('verification-report').textContent, /已核对逻辑引用数未核对/);
  assert.equal(h.elements.get('verification-body').value, '');
});
test('M4F later package, project and Vault scope guard prevents stale verification response overwrites', async () => {
  for (const change of ['package', 'project', 'vault']) {
    const h = evidenceHarness(); let resolve;
    h.setFetch(() => new Promise(r => { resolve = r; }));
    const first = h.ui.verifyEvidence('a'.repeat(64)); const firstResolve = resolve;
    if (change === 'package') {
      h.setFetch(async () => response(verificationFixture({packageId: 'b'.repeat(64), currentReuseStatus: 'stale'})));
      await h.ui.verifyEvidence('b'.repeat(64));
    } else if (change === 'project') { h.elements.get('search-project').value = 'other'; h.ui.invalidateEvidence(); }
    else { h.ui.state.vaultId = 'other'; h.ui.state.generation += 1; h.ui.invalidateEvidence(); }
    firstResolve(response(verificationFixture())); await first;
    assert.ok(!h.elements.get('verification-status').textContent.includes('本次复用核验通过'));
    assert.equal(h.elements.get('verification-body').value, '');
  }
});
test('M4F network, auth and integrity failures clear prior success and never automatically retry or save', async () => {
  for (const kind of ['network', 'token', 'integrity']) {
    const h = evidenceHarness(); h.setFetch(async () => response(verificationFixture())); await h.ui.verifyEvidence('a'.repeat(64));
    h.setFetch(async () => {
      if (kind === 'network') throw new TypeError('offline');
      return response({error: {code: kind === 'token' ? 'TOKEN_REQUIRED' : 'EVIDENCE_INTEGRITY', message: 'failed verification'}, currentReuseStatus: 'failed'}, kind === 'token' ? 403 : 409);
    });
    await h.ui.verifyEvidence('a'.repeat(64));
    assert.match(h.elements.get('verification-status').textContent, /failed.*未知/);
    assert.equal(h.elements.get('verification-report').textContent, '');
    assert.equal(h.elements.get('verification-body').value, '');
    assert.equal(h.elements.get('verification-body-label').hidden, true);
    assert.equal(h.ui.verificationState.result, null);
    assert.equal(h.requests.length, 2);
    assert.ok(!h.requests.some(r => r.options.method === 'POST'));
  }
});
test('M4F saved list button performs read-only verification by package id', async () => {
  const h = evidenceHarness();
  h.setFetch(async url => url.includes('/csv-evidence-verify?') ? response(verificationFixture()) : response({packages: [{id: 'a'.repeat(64), query: 'saved', status: 'partial', includedRecordCount: 1, path: '/original/path'}]}));
  await h.ui.refreshEvidenceList();
  const button = h.elements.get('evidence-list').children[0].children.at(-1);
  assert.equal(button.textContent, '核验复用');
  await button.emit('click');
  assert.match(h.elements.get('verification-status').textContent, /usable/);
  const url = h.requests.at(-1).url;
  assert.ok(url.includes('packageId=' + 'a'.repeat(64)));
  assert.ok(!url.includes('path='));
});


test('M4G unconfigured steward is a connected read-only state, not a navigation failure', async () => {
  const h = harness();
  h.ui.state.vaultId = null; h.ui.state.status = null;
  h.ui.workState.correction = null;
  h.setFetch(async (url, options) => {
    assert.notEqual(options.method, 'POST');
    if (url === '/api/vaults/test/steward') return response({error: {code: 'STEWARD_UNCONFIGURED', message: '该服务尚未启用管家运行目录'}}, 503);
    return readyFetch()(url);
  });
  await h.ui.openNavigation({vault: 'test', page: 'steward', project: ''}, {initial: true});
  assert.equal(h.ui.connectionState.phase, 'ready');
  assert.equal(h.ui.state.vaultId, 'test');
  assert.match(h.elements.get('steward-status').textContent, /管家未配置/);
  assert.doesNotMatch(h.elements.get('steward-status').textContent, /未知|恢复连接/);
  assert.doesNotMatch(h.elements.get('steward-model').textContent, /先选择有效记忆库/);
  assert.match(h.elements.get('steward-model').textContent, /管家未配置/);
  for (const id of ['steward-enqueue', 'steward-run', 'steward-pause']) assert.equal(h.elements.get(id).disabled, true);
  assert.equal(h.requests.some(r => r.url === '/api/models'), false);
  h.setFetch(readyFetch());
  await h.ui.refreshSteward();
  assert.equal(h.ui.workState.unconfigured, false);
  assert.match(h.elements.get('steward-status').textContent, /当前没有运行中的轮次/);
});

test('M4G network failure remains unknown and does not claim unconfigured', async () => {
  const h = harness();
  await assert.rejects(h.ui.refreshSteward());
  assert.equal(h.ui.workState.unconfigured, false);
  assert.match(h.elements.get('steward-status').textContent, /运行情况未知/);
  assert.equal(h.elements.get('steward-run').disabled, true);
});


test('M4G retained list button verifies explicitly after identity-checked reconnect without list reload', async () => {
  const h = evidenceHarness();
  const report = verificationFixture();
  h.setFetch(async url => url.includes('/csv-evidence-verify?') ? response(report) : response({packages: [{id: report.packageId, query: 'saved', status: 'partial', includedRecordCount: 1, path: '/original/path'}]}));
  await h.ui.refreshEvidenceList();
  const button = h.elements.get('evidence-list').children[0].children.at(-1);
  await button.emit('click');
  h.setFetch(async () => { throw new TypeError('offline'); });
  await button.emit('click');
  assert.match(h.elements.get('verification-status').textContent, /failed/);
  assert.equal(h.elements.get('verification-body').value, '');
  h.setFetch(readyFetch());
  await h.ui.reconnectService();
  const beforeClick = h.requests.length;
  assert.equal(h.elements.get('evidence-list').children[0].children.at(-1), button);
  assert.equal(h.elements.get('verification-body').value, '');
  h.setFetch(async url => { assert.ok(url.includes('/csv-evidence-verify?')); return response(report); });
  await button.emit('click');
  assert.equal(h.requests.length, beforeClick + 1);
  assert.match(h.elements.get('verification-status').textContent, /usable/);
  assert.equal(h.elements.get('verification-body').value, report.markdown);
  assert.equal(h.requests.filter(r => r.url.includes('/csv-evidence?')).length, 1);
  assert.ok(!h.requests.some(r => r.options.method === 'POST'));
  h.ui.state.vaults[0].vaultId = 'different-vault';
  const beforeMismatch = h.requests.length;
  await button.emit('click');
  assert.equal(h.requests.length, beforeMismatch);
});


test('M4G obsolete read failure cannot poison current connection after scope changes', async () => {
  for (const change of ['project', 'vault', 'page', 'superseded']) {
    for (const failure of ['timeout', 'network', 'token']) {
      const h = harness(); h.ui.state.page = 'search';
      h.elements.get('search-project').value = 'old';
      let rejectOld, respondOld, signal, current = true;
      h.setFetch((url, options) => { signal = options.signal; return new Promise((resolve, reject) => { respondOld = resolve; rejectOld = reject; }); });
      const old = h.ui.request('/api/vaults/test/context?project=old', {}, () => current);
      if (change === 'project') h.elements.get('search-project').value = 'new';
      if (change === 'vault') { h.ui.state.generation++; h.ui.state.vaultId = 'other'; }
      if (change === 'page') h.ui.state.page = 'records';
      if (change === 'superseded') current = false;
      if (failure === 'timeout') { h.fireTimeout(20000); assert.equal(signal.aborted, true); rejectOld(new DOMException('timeout', 'AbortError')); }
      if (failure === 'network') rejectOld(new TypeError('offline old request'));
      if (failure === 'token') respondOld(response({error: {code: 'TOKEN_REQUIRED'}}, 403));
      await assert.rejects(old);
      assert.equal(h.ui.connectionState.phase, 'ready', change + '/' + failure);
    }
  }
});

test('M4G current read timeout and uncertain write still mark connection failure', async () => {
  for (const writing of [false, true]) {
    const h = harness(); let rejectOld;
    h.setFetch(() => new Promise((resolve, reject) => { rejectOld = reject; }));
    const pending = h.ui.request('/api/vaults/test/context', writing ? {method:'POST', body:'{}'} : {});
    if (writing) h.ui.state.page = 'records';
    h.fireTimeout(20000); rejectOld(new DOMException('timeout', 'AbortError'));
    await assert.rejects(pending);
    assert.equal(h.ui.connectionState.phase, 'offline');
    assert.equal(h.requests.length, 1);
  }
});


test('M4G superseded CSV verification failure preserves current package and connected controls', async () => {
  for (const failure of ['timeout', 'network', 'token']) {
    const h = evidenceHarness(); let rejectOld, resolveOld;
    h.setFetch(() => new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; }));
    const old = h.ui.verifyEvidence('a'.repeat(64));
    h.setFetch(async () => response(verificationFixture({packageId: 'b'.repeat(64), markdown: '# current package'})));
    await h.ui.verifyEvidence('b'.repeat(64));
    if (failure === 'timeout') { h.fireTimeout(20000); rejectOld(new DOMException('timeout', 'AbortError')); }
    if (failure === 'network') rejectOld(new TypeError('old network failure'));
    if (failure === 'token') resolveOld(response({error: {code: 'TOKEN_REQUIRED'}}, 403));
    await old;
    assert.equal(h.ui.connectionState.phase, 'ready');
    assert.equal(h.elements.get('verification-body').value, '# current package');
    assert.match(h.elements.get('verification-status').textContent, /usable/);
    assert.equal(h.elements.get('evidence-prepare').disabled, false);
    assert.equal(h.requests.length, 2);
    h.setFetch(async () => { throw new TypeError('current network failure'); });
    await h.ui.verifyEvidence('b'.repeat(64));
    assert.equal(h.ui.connectionState.phase, 'offline');
    assert.equal(h.elements.get('verification-body').value, '');
    assert.match(h.elements.get('verification-status').textContent, /failed/);
    assert.ok(!h.requests.some(r => r.options.method === 'POST'));
  }
});
