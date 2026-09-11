'use strict';

const $ = (id) => document.getElementById(id);
const state = {token: null, vaultId: null, vaults: [], status: null, records: [], selected: null, page: 'steward', generation: 0, searchRequest: 0};
const pageTitles = {steward: '记忆管家', knowledge: '加工知识', records: '原始资料', search: '项目检索', import: '导入资料', codex: 'Codex 入口', models: '模型服务'};
const modelState = {profiles: [], primary: null, credentialStore: {}, runtimePath: null, selectedId: null, loaded: false, busy: false, dirty: false};
const workState = {project: '', status: null, unconfigured: false, knowledge: null, knowledgeLoad: 'idle', knowledgeError: '', historical: null, busy: false, poll: null, refreshId: 0, knowledgeRequest: 0, searchRequest: 0, queueEvent: null, correction: null, restore: null, statusFingerprint: null};
const navigationState = {applying: false, sequence: 0, ready: false, importDirty: false};
const connectionState = {phase: 'connecting', epoch: 0, reconnecting: false, pendingWrites: 0};
const parseLabels = {text: '文本可读取', jsonl: 'JSONL 可读取', unsupported: '解析未支持', 'invalid-jsonl': 'JSONL 格式无效', 'invalid-utf8': '文本编码无效'};

function currentVault() { return state.vaults.find((vault) => vault.id === state.vaultId) || null; }
function currentDataScope() { return state.status?.dataScope || currentVault()?.dataScope || 'synthetic'; }
function isHumanTrial() { return currentDataScope() === 'human-trial'; }
function scopeLabel(scope = currentDataScope()) { return scope === 'human-trial' ? '真实试用资料' : '合成测试资料'; }
function projectPlaceholder() { return isHumanTrial() ? '请选择项目' : '请选择合成项目'; }

function updateScopeCopy() {
  const human = isHumanTrial();
  $('stage-banner').textContent = human ? '真实试用库 · 原文与加工结果本机留存 · 模型只处理已授权的项目'
    : '专用合成测试库 · 原文与加工结果真实留存 · 管家只处理已授权的合成项目';
  $('steward-heading-copy').textContent = human ? '选择试用项目，将已有原文加入队列。每轮都有明确的任务数与时间上限。'
    : '选择合成项目，将已有原文加入队列。每轮都有明确的任务数与时间上限。';
  $('import-title').textContent = human ? '导入试用资料' : '导入测试资料';
  $('import-heading-copy').textContent = human ? '只保存你显式提交的低风险真实试用资料；已有来源的新版本以新增方式留存。'
    : '保留原文与来源。已有来源的新版本以新增方式留存。';
  $('scope-confirm-copy').textContent = human ? '我确认这是低风险真实试用资料，已排除密钥、隐私和敏感公司资料，并同意保存到本机试用库。'
    : '我确认提交的是明确标记的合成测试资料，不含真实个人或公司资料。';
  $('model-project-auth-title').textContent = human ? '真实试用项目处理授权' : '合成项目处理授权';
  $('model-project-auth-copy').textContent = human ? '授权绑定当前连接版本、记忆库和项目。真实试用资料只有在本处按项目确认后才允许模型处理。'
    : '授权绑定当前连接版本、记忆库和项目。仅允许合成资料；已有有效授权的任务运行时无需重复批准。';
  $('model-project-confirm-copy').textContent = human ? '我确认这是低风险真实试用项目，并授权该模型在上述范围内处理。'
    : '我确认这是合成项目，并授权该模型在上述范围内处理。';
  $('correction-text').placeholder = human ? '填写应替代当前条目的正确内容，并保留依据。不要粘贴密钥、隐私或敏感公司资料。'
    : '填写应替代当前条目的正确内容，例如：当前会议时间：周一 10:30。仅填写合成项目内容。';
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function showNotice(message, error = false, kind = '') {
  $('notice').textContent = message;
  $('notice').className = error ? 'notice error' : 'notice';
  $('notice').hidden = !message;
  $('notice').dataset.kind = kind;
}

function errorMessage(error) { return error?.message || '操作失败，请检查本地服务。'; }

async function request(path, options = {}, isCurrent = () => true) {
  const epoch = connectionState.epoch;
  const bootstrapRequest = path === '/api/bootstrap';
  const writing = !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase());
  const generation = state.generation, vault = state.vaultId, page = state.page, project = currentNavigationProject();
  // Obsolete reads must not mark the currently displayed scope disconnected.
  // Writes retain their existing uncertain-result/connection handling.
  const mayUpdateConnection = () => epoch === connectionState.epoch && (writing ||
    (generation === state.generation && vault === state.vaultId && page === state.page
      && project === currentNavigationProject() && isCurrent()));
  if (!bootstrapRequest && (connectionState.phase !== 'ready' || (connectionState.reconnecting && writing))) {
    const error = new Error(connectionState.phase === 'identity-mismatch' ? '记忆库身份已变化，已拒绝旧目标操作。请保留草稿后重新加载并核对记忆库。' : '连接尚未恢复，请点击“重新连接”。当前草稿和事件标识仍保留，未自动重试提交。');
    error.code = 'RECONNECT_REQUIRED';
    throw error;
  }
  const headers = {'Accept': 'application/json', ...options.headers};
  if (state.token) headers['X-Memory-Token'] = state.token;
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const timeoutMs = /^\/api\/models\/[a-zA-Z0-9_-]+\/check$/.test(path) ? 75000 : 20000;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  if (writing) connectionState.pendingWrites += 1;
  updateReconnectControl();
  try {
    const response = await fetch(path, {...options, headers, signal: controller.signal, cache: 'no-store', credentials: 'same-origin'});
    let result;
    try { result = await response.json(); }
    catch (error) {
      if (controller.signal.aborted || error.name === 'AbortError' || error instanceof TypeError) throw error;
      const invalid = new Error(`服务返回非 JSON 响应（HTTP ${response.status}）。请保留当前事件标识并核实提交结果。`);
      invalid.code = 'INVALID_HTTP_RESPONSE';
      throw invalid;
    }
    if (epoch !== connectionState.epoch) { const stale = new Error('该响应属于旧连接，已忽略。'); stale.code = 'STALE_CONNECTION'; throw stale; }
    if (!response.ok) {
      const code = result.error?.code || response.status;
      const error = new Error(code === 'TOKEN_REQUIRED' ? '服务会话已更新，请点击“重新连接”。草稿仍保留，本次请求未获执行许可。' : `${code}：${result.error?.message || '请求未成功'}`);
      error.responseBody = result;
      error.httpStatus = response.status;
      error.code = code;
      if (code === 'TOKEN_REQUIRED' && mayUpdateConnection()) markConnectionFailure('session-expired', error.message);
      throw error;
    }
    return result;
  } catch (cause) {
    if (typeof cause.httpStatus === 'number' || ['INVALID_HTTP_RESPONSE', 'STALE_CONNECTION'].includes(cause.code)) throw cause;
    const error = new Error(controller.signal.aborted ? '请求等待超时。请点击“重新连接”核验服务；提交结果尚未确认，未自动重试。' : '无法连接本地服务。请点击“重新连接”，保留当前草稿和事件标识；提交结果尚未确认。');
    error.code = controller.signal.aborted ? 'REQUEST_TIMEOUT' : 'NETWORK_FAILED';
    if (mayUpdateConnection()) markConnectionFailure('offline', error.message);
    throw error;
  } finally {
    clearTimeout(timeout);
    if (writing) connectionState.pendingWrites -= 1;
    updateReconnectControl();
  }
}

function updateReconnectControl() {
  $('reconnect-button').disabled = connectionState.phase === 'connecting' || connectionState.reconnecting || connectionState.pendingWrites > 0 || workState.busy || modelState.busy;
  $('reconnect-button').textContent = connectionState.reconnecting ? '正在核验连接…' : '重新连接';
}

function markConnectionFailure(phase, message) {
  connectionState.phase = phase;
  stopWorkPoll();
  $('connection-status').textContent = phase === 'identity-mismatch' ? '记忆库身份变化 · 已停止操作' : phase === 'session-expired' ? '会话已过期 · 请重新连接' : '连接中断 · 草稿保留';
  $('steward-status').textContent = '连接不可用，当前运行状态未知；请重新连接。';
  if (workState.project) { workState.knowledgeLoad = 'failed'; workState.knowledgeError = message; }
  updateWorkControls();
  updateModelControls();
}

async function reconnectService() {
  if (connectionState.reconnecting || connectionState.pendingWrites || workState.busy || modelState.busy) return;
  const alias = state.vaultId;
  const previousVault = alias ? state.vaults.find((vault) => vault.id === alias) : null;
  const expectedId = previousVault?.vaultId;
  const expectedPath = previousVault?.path;
  const generation = ++state.generation;
  const epoch = ++connectionState.epoch;
  connectionState.reconnecting = true;
  connectionState.phase = 'reconnecting';
  state.token = null;
  state.searchRequest += 1;
  workState.refreshId += 1;
  workState.knowledgeRequest += 1;
  workState.searchRequest += 1;
  workState.statusFingerprint = null;
  cancelNavigation();
  stopWorkPoll();
  updateReconnectControl();
  updateWorkControls();
  try {
    const result = await request('/api/bootstrap');
    if (epoch !== connectionState.epoch || generation !== state.generation || alias !== state.vaultId) return;
    const actual = result.vaults?.find((vault) => vault.id === alias);
    if (alias && (!expectedId || !expectedPath || actual?.vaultId !== expectedId || actual?.path !== expectedPath)) {
      const message = '同名记忆库的身份或目录已变化，或当前库未开放，已拒绝旧目标操作。草稿保留；请先保留草稿后重新加载并核对记忆库。';
      markConnectionFailure('identity-mismatch', message);
      showNotice(message, true);
      return;
    }
    if (typeof result.csrfToken !== 'string' || !result.csrfToken || !Array.isArray(result.vaults)) throw new Error('服务未返回有效的连接身份。');
    state.token = result.csrfToken;
    state.vaults = result.vaults;
    connectionState.phase = 'ready';
    $('connection-status').textContent = '● 已重新连接并核验记忆库身份';
    if (!alias) {
      $('vault-select').replaceChildren();
      const placeholder = node('option', result.vaults.length ? '请选择记忆库' : '没有已配置的记忆库');
      placeholder.value = '';
      $('vault-select').append(placeholder);
      for (const vault of result.vaults) {
        const option = node('option', vaultDisplayName(vault));
        option.value = vault.id;
        $('vault-select').append(option);
      }
      $('vault-select').disabled = !result.vaults.length;
      $('example-entry').hidden = !result.vaults.some((vault) => vault.id === 'm3');
      navigationState.ready = true;
      if (result.vaults.length && !hasNavigationDraft()) await openNavigation(parseNavigationQuery(window.location.search), {initial: true});
      if (epoch === connectionState.epoch) showNotice(state.vaultId ? '已恢复连接并打开定位。未自动提交任何内容。' : '连接已恢复。可从列表选择记忆库；未提交的表单保持原样。');
      return;
    }
    if (!state.status) {
      const status = await request(`/api/vaults/${encodeURIComponent(alias)}/status`);
      if (epoch !== connectionState.epoch || generation !== state.generation) return;
      renderStatus(status);
    }
    if (alias && workState.project) await refreshKnowledge();
    if (epoch !== connectionState.epoch || generation !== state.generation) return;
    if (alias) await refreshSteward();
    if (epoch === connectionState.epoch && generation === state.generation) showNotice('已重新连接同一记忆库。草稿和原事件仍保留，未自动提交；请核对后手动确认上次提交结果。');
  } catch (error) {
    if (epoch === connectionState.epoch && generation === state.generation) {
      if (connectionState.phase === 'reconnecting') markConnectionFailure('offline', errorMessage(error));
      showNotice(errorMessage(error), true);
    }
  } finally {
    if (epoch === connectionState.epoch) {
      connectionState.reconnecting = false;
      renderKnowledge();
      updateWorkControls();
      updateModelControls();
      updateReconnectControl();
    }
  }
}

function vaultPath(suffix = '') {
  if (!state.vaultId) throw new Error('请先选择记忆库。');
  return `/api/vaults/${encodeURIComponent(state.vaultId)}${suffix}`;
}

function showPage(page, {refresh = true} = {}) {
  if (!Object.prototype.hasOwnProperty.call(pageTitles, page)) throw new Error('链接中的页面不存在，请使用页面导航。');
  if (state.page !== page) invalidateEvidence('页面已变化，请重新准备 CSV 证据包。');
  state.page = page;
  for (const name of Object.keys(pageTitles)) $('page-' + name).hidden = name !== page;
  for (const button of document.querySelectorAll('[data-page]')) {
    const selected = button.dataset.page === page;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-current', selected ? 'page' : 'false');
  }
  $('page-title').textContent = pageTitles[page];
  if (refresh && page === 'models' && !modelState.loaded && !modelState.busy) runModelAction(() => refreshModels());
  if (refresh && state.vaultId && page === 'steward') refreshSteward().catch((error) => showNotice(errorMessage(error), true));
  if (refresh && state.vaultId && page === 'search') { refreshContexts(); refreshEvidenceList(); }
  if (refresh && state.vaultId && page === 'knowledge') refreshKnowledge().catch((error) => showNotice(errorMessage(error), true));
  syncNavigationLocation();
}

function vaultDisplayName(vault) {
  const labels = {m3: '管家体验库（m3）', test: '基础存取库（test）', empty: '空库（empty）'};
  return Object.prototype.hasOwnProperty.call(labels, vault.id) ? labels[vault.id] : `${vault.name || '记忆库'}（${vault.id}）`;
}

function parseNavigationQuery(search) {
  const params = new URLSearchParams(search);
  const allowed = ['vault', 'page', 'project'];
  if ([...params.keys()].some((key) => !allowed.includes(key)) || allowed.some((key) => params.getAll(key).length > 1)) throw new Error('定位链接只接受唯一的 vault、page、project 参数。');
  const target = Object.fromEntries(allowed.map((key) => [key, params.has(key) ? params.get(key) : null]));
  if (target.vault !== null && !/^[a-zA-Z0-9_-]+$/.test(target.vault)) throw new Error('链接中的记忆库标识无效；请从库下拉列表选择。');
  if (target.page !== null && !Object.prototype.hasOwnProperty.call(pageTitles, target.page)) throw new Error('链接中的页面不存在；请从页面导航选择。');
  if (target.project !== null && (!target.project || target.project.length > 200 || /[\u0000-\u001f\u007f]/.test(target.project))) throw new Error('链接中的项目名称无效；未切换到其他项目。');
  return target;
}

function navigationQuery(vault, page, project = '') {
  const params = new URLSearchParams({vault, page});
  if (project) params.set('project', project);
  return '?' + params.toString();
}

function currentNavigationProject() {
  if (state.page === 'records') return $('records-project').value;
  if (state.page === 'search') return $('search-project').value;
  return workState.project;
}

function syncNavigationLocation() {
  if (!navigationState.ready || navigationState.applying || !state.vaultId) return;
  const query = navigationQuery(state.vaultId, state.page, currentNavigationProject());
  if (window.location.search !== query) window.history.replaceState(null, '', window.location.pathname + query);
}

function hasNavigationDraft() {
  const importFilled = ['import-project', 'import-source-id', 'import-locator', 'import-filename', 'import-content'].some((id) => $(id).value.trim()) || $('import-file').files?.length;
  return Boolean(evidenceState.pending) || Boolean(contextState.pending) || modelState.dirty || Boolean($('model-secret').value) ||
    Boolean($('correction-text').value.trim() && !workState.correction?.done) ||
    Boolean($('knowledge-restore-reason').value.trim() && workState.restore) ||
    Boolean(navigationState.importDirty && importFilled);
}

function cancelNavigation() {
  navigationState.sequence += 1;
  navigationState.applying = false;
  $('open-steward-example').disabled = false;
}

async function openNavigation(target, {initial = false} = {}) {
  const vaultId = target.vault ?? state.vaultId ?? state.vaults[0]?.id;
  const page = target.page ?? 'steward';
  const project = target.project ?? '';
  if (!state.vaults.some((vault) => vault.id === vaultId)) throw new Error(`链接指定的记忆库“${vaultId || '未提供'}”未在当前服务开放；未切换到其他库。`);
  if (!Object.prototype.hasOwnProperty.call(pageTitles, page)) throw new Error('链接中的页面不存在；未切换页面。');
  if (!initial && (connectionState.reconnecting || workState.busy || modelState.busy || hasNavigationDraft())) throw new Error('当前有未完成操作或未提交表单，内容已保留。请先完成或清空表单，再打开定位入口。');
  const sequence = ++navigationState.sequence;
  navigationState.applying = true;
  $('open-steward-example').disabled = true;
  try {
    if (project) {
      const status = state.vaultId === vaultId && state.status ? state.status : await request(`/api/vaults/${encodeURIComponent(vaultId)}/status`);
      if (sequence !== navigationState.sequence) return;
      if (!(status.projects || []).includes(project)) throw new Error(`“${vaultDisplayName(state.vaults.find((vault) => vault.id === vaultId))}”中没有项目“${project}”；未打开其他项目。`);
    }
    if (state.vaultId !== vaultId || !state.status) {
      $('vault-select').value = vaultId;
      await selectVault({page});
      if (sequence !== navigationState.sequence || state.vaultId !== vaultId) return;
    }
    if (project && !(state.status?.projects || []).includes(project)) throw new Error(`“${vaultDisplayName(state.vaults.find((vault) => vault.id === vaultId))}”中没有项目“${project}”；未打开其他项目。`);
    showPage(page, {refresh: false});
    if (workState.project !== project || (project && !workState.knowledge)) await setWorkProject(project, {loadKnowledge: page === 'knowledge' || page === 'steward'});
    if (sequence !== navigationState.sequence || state.vaultId !== vaultId || workState.project !== project) return;
    if (page === 'records' || page === 'search') {
      if (page === 'search' && $('search-project').value !== project) { clearContextReceipt(); clearEvidenceReceipt(); }
      $(page === 'records' ? 'records-project' : 'search-project').value = project;
      if (page === 'records') await refreshRecords({resetDetail: true});
      else { invalidateContext(); invalidateEvidence(); await Promise.all([refreshContexts(), refreshEvidenceList()]); }
    }
    if (sequence !== navigationState.sequence) return;
    navigationState.applying = false;
    syncNavigationLocation();
    if (page === 'steward' && workState.unconfigured) showNotice('STEWARD_UNCONFIGURED：该服务尚未启用管家运行目录', true);
    else showNotice('');
  } finally {
    if (sequence === navigationState.sequence) navigationState.applying = false;
    $('open-steward-example').disabled = false;
  }
}

function fillProjects(id, projects, placeholder) {
  const select = $(id);
  const previous = select.value;
  select.replaceChildren(node('option', placeholder));
  select.firstElementChild.value = '';
  for (const project of projects) {
    const option = node('option', project);
    option.value = project;
    select.append(option);
  }
  if (projects.includes(previous)) select.value = previous;
}

function shellQuote(value) {
  return "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
}

function renderCodexCommands(status) {
  if (!status.clientPath || !status.serviceUrl || !state.vaultId) {
    $('codex-search-command').textContent = '服务尚未提供客户端路径或地址，无法生成真实检索命令。';
    $('codex-context-command').textContent = '服务尚未提供上下文命令所需信息。';
    $('codex-submit-command').textContent = '服务尚未提供客户端路径或地址，无法生成真实提交命令。';
    return;
  }
  const client = `python3 ${shellQuote(status.clientPath)} --url ${shellQuote(status.serviceUrl)} --vault ${shellQuote(state.vaultId)}`;
  const project = isHumanTrial() ? '你的试用项目' : '你的合成项目';
  $('codex-context-command').textContent = `${client} context --project ${shellQuote(project)} --query ${shellQuote('任务问题')} --max-bytes 8192\n${client} save-context ${shellQuote('context-payload.json')}\n${client} contexts --project ${shellQuote(project)}`;
  $('codex-search-command').textContent = `${client} search --project ${shellQuote(project)} --query ${shellQuote('关键词')}`;
  $('codex-submit-command').textContent = `${client} submit ${shellQuote('payload.json')}`;
}

function renderCodexPayload() {
  const human = isHumanTrial();
  const payload = {
    eventId: '替换为本次稳定事件ID',
    project: human ? '你的试用项目' : '你的合成项目',
    source: {id: '替换为工作记录的来源ID', tool: 'Codex', locator: human ? 'local://替换为本次试用定位' : 'synthetic://替换为本次测试定位', recordedAt: null, sessionId: null},
    filename: human ? 'codex-trial-note.md' : 'codex-test-result.md',
    content: human ? '请填写本次实际读取的来源、版本、行号、结论与未验证项。不要粘贴密钥、隐私或敏感公司资料。' : '【合成测试】请填写本次实际读取的来源、版本、行号、结论与未验证项。此处是待填写示例，不是测试通过证明。',
    expectedVersion: 0,
    kind: 'work-record',
    dataScope: human ? 'human-trial' : 'synthetic',
    synthetic: !human,
  };
  if (human) payload.humanTrial = true;
  $('codex-payload').textContent = JSON.stringify(payload, null, 2);
}

function renderStatus(status) {
  state.status = status;
  if (status.dataScope && currentVault()) currentVault().dataScope = status.dataScope;
  updateScopeCopy();
  const displayName = vaultDisplayName(state.vaults.find((vault) => vault.id === state.vaultId) || {id: state.vaultId, name: status.name});
  $('current-vault-name').textContent = displayName;
  $('vault-path').textContent = status.path;
  $('footer-vault').textContent = displayName;
  $('codex-entry').textContent = status.entryPath;
  renderCodexCommands(status);
  renderCodexPayload();
  const count = Array.isArray(status.records) ? status.records.length : status.records;
  $('record-count').textContent = `${count ?? '—'} 条版本记录`;
  const index = typeof status.indexStatus === 'string' ? status.indexStatus : JSON.stringify(status.indexStatus ?? '未知');
  $('index-status').textContent = `索引：${index === 'ready' ? '可用' : index}`;
  fillProjects('records-project', status.projects || [], '全部项目（仅浏览）');
  fillProjects('search-project', status.projects || [], '请选择项目');
  for (const id of ['steward-project', 'knowledge-project', 'model-project-scope']) fillProjects(id, status.projects || [], projectPlaceholder());
  if (!(status.projects || []).includes(workState.project)) workState.project = '';
  $('steward-project').value = $('knowledge-project').value = workState.project;
  $('model-project-vault').textContent = `授权记忆库：${status.name}（${state.vaultId}）`;
  updateWorkControls();
}

function renderRecords() {
  const list = $('record-list');
  list.replaceChildren();
  if (!state.records.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('h3', $('records-project').value ? '此项目还没有资料' : '记忆库为空'), node('p', `可从“导入资料”保存第一条${scopeLabel()}。`));
    list.append(empty);
    return;
  }
  for (const record of state.records) {
    const button = node('button', undefined, 'record-row');
    button.type = 'button';
    button.dataset.recordId = record.id;
    button.dataset.version = String(record.version);
    button.classList.toggle('selected', state.selected?.id === record.id && state.selected?.version === record.version);
    button.append(node('b', record.filename), node('small', `${record.project} · v${record.version} · ${record.kind === 'work-record' ? '测试工作记录' : '原始资料'}`), node('small', record.status === 'historical-source-version' ? '历史来源版本' : '最新来源版本'));
    button.addEventListener('click', () => loadRecord(record.id, record.version).catch((error) => showNotice(errorMessage(error), true)));
    list.append(button);
  }
}

function addMetadata(list, key, value) {
  list.append(node('dt', key), node('dd', value === null || value === undefined || value === '' ? '缺失 / 未提供' : String(value)));
}

function renderDetail(record, content) {
  const detail = $('record-detail');
  detail.replaceChildren();
  const heading = node('div', undefined, 'reader-title');
  heading.append(node('h2', record.filename), node('span', `v${record.version}`, 'tag blue'));
  const tags = node('div', undefined, 'reader-meta');
  tags.append(node('span', record.kind === 'work-record' ? '工作记录' : '原始资料', 'tag'), node('span', record.status === 'historical-source-version' ? '历史来源版本' : '最新来源版本', 'tag'), node('span', (/\.csv$/i.test(record.filename) ? 'CSV 尚未纳入文本索引' : parseLabels[record.parseStatus] || record.parseStatus), ['text', 'jsonl'].includes(record.parseStatus) ? 'tag green' : 'tag amber'), node('span', scopeLabel(record.dataScope || (record.synthetic ? 'synthetic' : 'unknown')), record.dataScope === 'human-trial' ? 'tag blue' : record.synthetic ? 'tag' : 'tag amber'));
  const metadata = node('dl', undefined, 'metadata');
  addMetadata(metadata, '项目', record.project);
  addMetadata(metadata, '记录 ID', record.id);
  addMetadata(metadata, '文件路径', record.path);
  addMetadata(metadata, '来源 ID', record.source?.id);
  addMetadata(metadata, '来源工具', record.source?.tool);
  addMetadata(metadata, '来源定位', record.source?.locator);
  addMetadata(metadata, '原始时间', record.source?.recordedAt);
  addMetadata(metadata, '会话 ID', record.source?.sessionId);
  addMetadata(metadata, '保存时间', record.createdAt);
  addMetadata(metadata, '事件 ID', record.eventId);
  addMetadata(metadata, '原文校验', record.sha256);
  addMetadata(metadata, '缺失信息', record.missing?.length ? record.missing.join('；') : '无登记缺失项');
  detail.append(heading, tags, metadata, node('h3', '原文 · 按保存内容显示', 'content-heading'));
  if (content === null || content === undefined) detail.append(node('p', (/\.csv$/i.test(record.filename) ? 'CSV 原文件已保留；下方提供独立结构视图，未纳入旧文本索引或知识加工。' : '此文件尚不支持文本解析。原文件已保留；请按上面的文件路径读取。'), 'empty'));
  else detail.append(node('pre', content, 'raw-content'));
}

function renderCsvTable(container, result) {
  const {record, table: view} = result;
  container.replaceChildren(node('h3', 'CSV 结构视图'));
  container.append(node('p', `来源 v${view.sourceVersion} · ${view.sourceSHA} · 解析器 ${view.parserVersion}`, 'help mono'));
  container.append(node('p', '合成表格；只显示文本，不执行公式或材料指令；未加工为当前知识。旧文本索引不包含 CSV。', 'scope-note'));
  if (view.parseStatus !== 'parsed') {
    container.append(node('p', `原文已保存，表格未解析：${view.error.code} · ${view.error.message}`, 'scope-note'));
    return;
  }
  container.append(node('p', `CSV 结构视图已解析：${view.dataRecordCount} 条数据记录，${view.columnCount} 列。${record.status === 'historical-source-version' ? '这是历史来源版本。' : '这是最新来源版本（本次读取时）。'}`, 'help'));
  const tableWrap = node('div', undefined, 'csv-table-wrap');
  const paging = node('div', undefined, 'actions');
  const previous = node('button', '上一页', 'button'), next = node('button', '下一页', 'button');
  previous.type = next.type = 'button';
  const pageLabel = node('span', '', 'help');
  paging.append(previous, pageLabel, next);
  const location = node('p', '点击表头或单元格，查看逻辑记录、列序号与原文物理行。', 'help');
  const excerpt = node('pre', '', 'raw-content');
  const lines = view.rawText.split('\n');
  const raw = node('details'), rawLines = node('div', undefined, 'csv-raw-lines');
  const count = lines.length - (lines[lines.length - 1] === '' ? 1 : 0);
  rawLines.append(node('pre', Array.from({length:count}, (_,i)=>String(i+1)).join('\n'), 'csv-line-numbers'), node('pre', view.rawText, 'csv-raw-text'));
  raw.append(node('summary', '完整原文（保留保存内容，左侧为物理行号）'), rawLines);
  function locate(row, column) {
    location.textContent = `逻辑记录 ${row.recordNumber}（含表头） · 第 ${column} 列 · 原文 L${row.lineStart}–L${row.lineEnd} · ${record.path} · v${view.sourceVersion}。下方为整个记录的原文范围，非单元格字符区间。`;
    excerpt.textContent = lines.slice(row.lineStart - 1, row.lineEnd).join('\n');
    location.scrollIntoView({block:'nearest'});
  }
  let page = 0;
  const pages = Math.max(1, Math.ceil(view.rows.length / 50));
  function renderPage() {
    const table = node('table', undefined, 'csv-table');
    const head = node('thead'), header = node('tr');
    for (const [i, value] of view.header.cells.entries()) {
      const th = node('th'), button = node('button', `第 ${i + 1} 列 · ${value || '（空表头）'}`, 'csv-cell');
      button.type = 'button'; button.addEventListener('click', () => locate(view.header, i + 1));
      th.append(button); header.append(th);
    }
    head.append(header); table.append(head);
    const body = node('tbody');
    for (const row of view.rows.slice(page * 50, (page + 1) * 50)) {
      const tr = node('tr');
      for (const [i, value] of row.cells.entries()) {
        const td = node('td'), button = node('button', value === '' ? '（空字段）' : value, 'csv-cell');
        button.type = 'button'; button.setAttribute('aria-label', `记录 ${row.recordNumber} 第 ${i + 1} 列：${value === '' ? '空字段' : value}`);
        button.addEventListener('click', () => locate(row, i + 1));
        td.append(button); tr.append(td);
      }
      body.append(tr);
    }
    table.append(body); tableWrap.replaceChildren(table);
    pageLabel.textContent = `第 ${page + 1} / ${pages} 页，每页最多 50 条数据记录`;
    previous.disabled = page === 0; next.disabled = page + 1 >= pages;
  }
  previous.addEventListener('click', () => {if (page > 0) {page--; renderPage();}});
  next.addEventListener('click', () => {if (page + 1 < pages) {page++; renderPage();}});
  container.append(tableWrap, paging, location, excerpt, raw); renderPage();
}

async function loadRecord(id, version, {switchPage = false} = {}) {
  const generation = state.generation;
  const selected = {id, version};
  state.selected = selected;
  const result = await request(vaultPath(`/records/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`));
  if (generation !== state.generation || state.selected !== selected) return;
  renderDetail(result.record, result.content);
  renderRecords();
  if (switchPage) showPage('records');
  if (/\.csv$/i.test(result.record.filename)) {
    const tableArea = node('section', undefined, 'csv-view');
    tableArea.append(node('p', '正在读取 CSV 结构视图；原文登记保持不变。', 'help'));
    $('record-detail').append(tableArea);
    try {
      const table = await request(vaultPath(`/records/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/table`) + '?' + new URLSearchParams({project: result.record.project}));
      if (generation !== state.generation || state.selected !== selected) return;
      renderCsvTable(tableArea, table);
    } catch (error) {
      if (generation === state.generation && state.selected === selected) tableArea.replaceChildren(node('p', `CSV 视图读取失败：${errorMessage(error)} 原文登记仍保留。`, 'scope-note'));
    }
  }
}

async function refreshRecords({resetDetail = false} = {}) {
  const generation = state.generation;
  const project = $('records-project').value;
  const result = await request(vaultPath('/records') + (project ? `?project=${encodeURIComponent(project)}` : ''));
  if (generation !== state.generation || project !== $('records-project').value) return;
  state.records = result.records;
  if (resetDetail || !state.records.some((record) => record.id === state.selected?.id && record.version === state.selected?.version)) {
    state.selected = null;
    const empty = node('div', undefined, 'empty');
    empty.append(node('h2', '选择一条资料'), node('p', '这里显示实际保存的原文、来源及版本。'));
    $('record-detail').replaceChildren(empty);
  }
  renderRecords();
}

async function refreshVault() {
  const generation = state.generation;
  const status = await request(vaultPath('/status'));
  if (generation !== state.generation) return;
  renderStatus(status);
  await refreshRecords();
  $('connection-status').textContent = '● 本地服务已响应';
}

async function selectVault({page = state.page} = {}) {
  if (!state.vaults.some((vault) => vault.id === $('vault-select').value)) throw new Error('请选择当前服务已开放的记忆库。');
  invalidateEvidence('记忆库已切换，请重新准备 CSV 证据包。');
  evidenceState.listRequest += 1;
  $('evidence-list').replaceChildren();
  clearEvidenceReceipt();
  invalidateContext('记忆库已切换，请重新准备任务上下文。');
  contextState.listRequest += 1;
  $('context-list').replaceChildren();
  contextState.receipt = null;
  $('context-receipt').hidden = true;
  state.generation += 1;
  const generation = state.generation;
  state.searchRequest += 1;
  state.vaultId = $('vault-select').value;
  updateScopeCopy();
  state.selected = null;
  state.status = null;
  state.records = [];
  resetWorkScope();
  fillProjects('records-project', [], '全部项目（仅浏览）');
  fillProjects('search-project', [], '请选择项目');
  $('current-vault-name').textContent = $('vault-select').selectedOptions[0]?.textContent || '本地记忆库';
  $('vault-path').textContent = '正在读取当前记忆库目录…';
  $('record-count').textContent = '版本记录待读取';
  $('index-status').textContent = '索引状态待读取';
  $('codex-entry').textContent = '当前记忆库入口待读取';
  $('codex-search-command').textContent = '当前记忆库检索命令待读取';
  $('codex-context-command').textContent = '当前记忆库上下文命令待读取';
  $('codex-submit-command').textContent = '当前记忆库提交命令待读取';
  $('footer-vault').textContent = '正在切换记忆库';
  $('record-list').replaceChildren(node('div', '正在读取当前记忆库记录…', 'empty'));
  $('record-detail').replaceChildren(node('div', '选择一条资料后查看原文与来源。', 'empty'));
  $('search-results').replaceChildren(node('div', '请在当前记忆库选择项目并检索。', 'empty'));
  $('search-count').textContent = '尚未检索';
  $('import-result').hidden = true;
  showNotice('');
  await refreshVault();
  if (generation !== state.generation) return;
  if (page === 'steward') await refreshSteward();
  if (generation !== state.generation) return;
  if ((page === 'models' || (page === 'steward' && !workState.unconfigured)) && !modelState.loaded && !modelState.dirty && !modelState.busy) {
    try { await refreshModels(); } catch (error) { showNotice(errorMessage(error), true); }
  }
  if (generation === state.generation) syncNavigationLocation();
}

async function search(event) {
  event.preventDefault();
  const project = $('search-project').value;
  const query = $('search-query').value.trim();
  if (!project || !query) { showNotice('必须明确选择一个项目并输入关键词。', true); return; }
  const generation = state.generation;
  const requestId = ++state.searchRequest;
  const button = event.submitter;
  if (button) button.disabled = true;
  $('search-count').textContent = '检索中…';
  try {
    const params = new URLSearchParams({project, q: query});
    if ($('search-history').checked) params.set('history', '1');
    const result = await request(vaultPath('/search') + '?' + params);
    if (generation !== state.generation || requestId !== state.searchRequest) return;
    $('search-count').textContent = `${result.total} 条匹配 · ${project}`;
    $('search-results').replaceChildren();
    if (!result.matches.length) {
      const empty = node('div', undefined, 'empty');
      empty.append(node('h3', '没有匹配资料'), node('p', `项目“${project}”中未找到“${query}”。没有证据时不补造答案。`));
      $('search-results').append(empty);
    }
    for (const match of result.matches) {
      const item = node('button', undefined, 'search-result');
      item.type = 'button';
      const heading = node('div', undefined, 'search-result-heading');
      heading.append(node('h3', match.filename), node('span', `v${match.version}`, 'tag'), node('span', `L${match.lineStart}–${match.lineEnd}`, 'tag blue'));
      item.append(heading, node('p', match.quote), node('small', `${match.project} · 来源 ${match.source?.id ?? match.source ?? '缺失'}`), node('small', `${match.path}:${match.lineStart}`));
      item.addEventListener('click', () => loadRecord(match.recordId, match.version, {switchPage: true}).catch((error) => showNotice(errorMessage(error), true)));
      $('search-results').append(item);
    }
    showNotice('');
  } catch (error) {
    if (generation !== state.generation || requestId !== state.searchRequest) return;
    $('search-count').textContent = '检索失败';
    $('search-results').replaceChildren(node('div', '当前检索未取得有效结果，请查看上方错误信息。', 'empty'));
    showNotice(errorMessage(error), true);
  } finally { if (button) button.disabled = false; }
}

const verificationState = {requestId: 0, packageId: null, result: null};
const evidenceState = {listRequest: 0, requestId: 0, preview: null, loading: false, saving: false, pending: null, receipt: null};
const contextState = {listRequest: 0, requestId: 0, preview: null, loading: false, saving: false, pending: null, receipt: null};

function pathCopyControls(path, label = '复制文件路径') {
  const controls = node('div', undefined, 'path-copy-controls');
  const copy = node('button', label, 'button'); copy.type = 'button';
  const manual = node('button', '选中路径手动复制', 'button'); manual.type = 'button';
  const field = node('textarea', undefined, 'path-copy-text');
  field.value = path; field.readOnly = true; field.rows = 3; field.hidden = true;
  field.setAttribute('aria-label', '待复制的完整文件路径'); field.spellcheck = false;
  const status = node('p', '', 'help'); status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  function selectPath() {
    field.hidden = false; field.focus(); field.select();
    field.setSelectionRange(0, field.value.length);
  }
  manual.addEventListener('click', () => {
    selectPath();
    status.textContent = '完整路径已选中，请按 ⌘C（Mac）或 Ctrl+C 复制，再粘贴核对。';
  });
  copy.addEventListener('click', async () => {
    if (copy.disabled) return;
    copy.disabled = true; status.textContent = '正在请求浏览器复制；也可选中路径手动复制。';
    async function verified() {
      try {
        return typeof navigator.clipboard?.readText === 'function' &&
          await navigator.clipboard.readText() === path;
      } catch { return false; }
    }
    try {
      if (typeof navigator.clipboard?.writeText !== 'function') throw new Error('CLIPBOARD_UNAVAILABLE');
      await navigator.clipboard.writeText(path);
      if (!await verified()) throw new Error('CLIPBOARD_UNVERIFIED');
      status.textContent = '浏览器剪贴板回读与完整路径一致，请在目标位置粘贴核对。';
    } catch (error) {
      selectPath();
      let copied = false;
      try { copied = typeof document.execCommand === 'function' && document.execCommand('copy') === true; }
      catch { /* Keep the visible selection available for a keyboard copy. */ }
      if (copied && await verified()) {
        status.textContent = '备用复制后，浏览器剪贴板回读与完整路径一致，请在目标位置粘贴核对。';
      } else {
        // A resolved write or legacy true result does not prove clipboard delivery.
        selectPath();
        status.textContent = '自动复制未确认：剪贴板内容不一致或无法读取。完整路径已选中，请按 ⌘C（Mac）或 Ctrl+C 复制，再粘贴核对。';
      }
    } finally { copy.disabled = false; }
  });
  controls.append(copy, manual, field, status);
  return controls;
}

function clearContextReceipt() {
  contextState.receipt = null;
  $('context-receipt').hidden = true;
  $('context-saved-path').textContent = '';
  $('context-saved-status').textContent = '';
  $('context-copy-tools').replaceChildren();
}

function contextScope() {
  return {vault: state.vaultId, project: $('search-project').value, query: $('context-query').value.trim(), maxBytes: Number($('context-budget').value)};
}
function contextScopeKey() { return JSON.stringify(contextScope()); }
function invalidateContext(message = '条件已变化，请重新准备任务上下文。') {
  contextState.requestId += 1;
  contextState.preview = null;
  contextState.loading = false;
  $('context-preview').value = '';
  $('context-results').replaceChildren();
  $('context-warnings').replaceChildren();
  $('context-status').textContent = message;
  updateContextControls();
}
function updateContextControls() {
  const ready = connectionState.phase === 'ready' && !connectionState.reconnecting && Boolean(state.vaultId);
  const locked = contextState.saving || Boolean(contextState.pending);
  $('context-prepare').disabled = !ready || locked || contextState.loading || workState.busy;
  $('context-save').disabled = !ready || contextState.saving || workState.busy || (!contextState.preview && !contextState.pending);
  $('context-save').textContent = contextState.saving ? '正在保存…' : contextState.pending ? '确认上次上下文保存结果' : '保存上下文文件';
  for (const id of ['context-query', 'context-budget']) $(id).disabled = locked;
  updateEvidenceControls();
  if (contextState.pending) $('context-status').textContent = '上次保存结果尚未确认。请重新连接后显式确认；将沿用原事件和原快照，不会自动提交。';
}
function contextDiagnosticsText(d) {
  if (!d) return '该快照未提供诊断字段；请重新准备以查看选择原因。';
  const reason = d.exclusionReason === 'no-current-knowledge' ? '尚无当前知识。' :
    d.exclusionReason === 'knowledge-not-current' ? '知识不是当前可用状态，整体排除。' : '';
  return `项目 ${d.scopeProject}：知识条目 ${d.availableEntries}，词法检查 ${d.evaluatedEntries}，匹配 ${d.matchedEntries}，纳入 ${d.includedEntries}，预算排除 ${d.budgetExcludedEntries}，无词法交集 ${d.noLexicalMatchEntries}，状态排除 ${d.statusExcludedEntries}。${reason}${d.notice}`;
}
function contextStaleText(reasons) {
  const labels = {'source-changed': '来源已变化', 'knowledge-version-changed': '知识版本已变化', 'knowledge-status-changed': '知识状态已变化'};
  return (reasons || []).map(code => labels[code] || '需重新核对').join('；') || '当前知识已变化';
}
async function prepareContext(event) {
  event?.preventDefault();
  if (contextState.saving || contextState.pending || workState.busy) return;
  const scope = contextScope();
  if (!scope.project || !scope.query || scope.query.length > 200) { showNotice('请选择项目，并填写 1 至 200 字的任务问题。', true); return; }
  invalidateContext('正在检索当前知识并准备引用…');
  const requestId = contextState.requestId, generation = state.generation, key = contextScopeKey();
  contextState.loading = true;
  updateContextControls();
  try {
    const result = await request(vaultPath('/context') + '?' + new URLSearchParams({project: scope.project, q: scope.query, maxBytes: scope.maxBytes}), {},
      () => requestId === contextState.requestId && key === contextScopeKey());
    if (generation !== state.generation || requestId !== contextState.requestId || key !== contextScopeKey()) return;
    contextState.preview = {result, key};
    $('context-preview').value = result.markdown;
    $('context-status').textContent = `${scope.project} · ${result.knowledgeStatus === 'current' ? `当前知识 v${result.knowledgeVersion}` : result.knowledgeStatus === 'pending-review' ? '知识待复核，未提供旧结论' : '尚无当前知识'} · ${result.results.length} 条纳入 / ${result.totalMatches} 条匹配 · ${result.bytes} / ${result.maxBytes} 字节`;
    const warnings = [contextDiagnosticsText(result.diagnostics), ...(result.warnings || [])];
    if (result.omittedCount) warnings.push(`预算内未纳入 ${result.omittedCount} 条匹配。可提高预算后重新准备；引用不会被截断。`);
    if (!result.results.length) warnings.push(result.totalMatches > 0 ? '本次有词法匹配，但预算内未纳入引用；请提高预算重新准备，不会截断或补造答案。' : '当前加工知识无匹配，不代表原文不存在；下方可检索原文。保存文件会保留缺失说明，不会补造答案。');
    for (const warning of warnings) $('context-warnings').append(node('p', warning, 'scope-note'));
    for (const entry of result.results) {
      const card = node('article', undefined, 'knowledge-card');
      card.append(node('p', entry.quote, 'knowledge-text'), node('p', `匹配词：${(entry.matchedTerms || []).join('、')} · 词法分数 ${entry.score}`, 'help'), renderCitation({...entry, text: entry.quote}, entry.knowledgeVersion));
      $('context-results').append(card);
    }
  } catch (error) {
    if (generation === state.generation && requestId === contextState.requestId) { $('context-status').textContent = '未取得有效上下文，请核对连接后重新准备。'; showNotice(errorMessage(error), true); }
  } finally {
    if (requestId === contextState.requestId) { contextState.loading = false; updateContextControls(); }
  }
}
async function saveContext() {
  if (contextState.saving || workState.busy) return;
  if (!contextState.pending) {
    const preview = contextState.preview;
    if (!preview || preview.key !== contextScopeKey()) { invalidateContext(); return; }
    const scope = contextScope();
    contextState.pending = {endpoint: vaultPath('/contexts'), body: {eventId: `context-ui-${crypto.randomUUID()}`, project: scope.project, query: scope.query, maxBytes: scope.maxBytes, expectedSnapshotId: preview.result.snapshotId}};
  }
  const pending = contextState.pending;
  $('context-event').textContent = pending.body.eventId;
  contextState.saving = true;
  updateWorkControls();
  $('vault-select').disabled = true;
  updateContextControls();
  try {
    const result = await request(pending.endpoint, {method: 'POST', body: JSON.stringify(pending.body)});
    contextState.receipt = result;
    contextState.pending = null;
    $('context-receipt').hidden = false;
    $('context-saved-path').textContent = result.path;
    $('context-copy-tools').replaceChildren(pathCopyControls(result.path, '复制上下文路径'));
    $('context-saved-status').textContent = `${result.duplicate ? '已确认此前保存' : '已保存真实上下文文件'}${result.stale ? `；${contextStaleText(result.staleReasons)}，此文件是历史快照，使用前请重新准备。` : '；这是生成时的快照，后续知识变化不会改写该文件。'}`;
    invalidateContext('上下文文件已保存。请让 Codex 读取下方路径；再次保存前需重新准备。');
    await refreshContexts();
  } catch (error) {
    if (typeof error.httpStatus === 'number' && error.httpStatus < 500) {
      contextState.pending = null;
      invalidateContext(error.code === 'CONTEXT_STALE' ? '知识或资料已变化，旧快照不能保存。请重新准备任务上下文。' : '本次保存被服务拒绝，请重新准备后核对。');
    }
    showNotice(errorMessage(error), true);
  } finally {
    contextState.saving = false;
    updateWorkControls();
    $('vault-select').disabled = workState.busy || modelState.busy;
    updateContextControls();
  }
}

async function refreshContexts() {
  const project = $('search-project').value, generation = state.generation, requestId = ++contextState.listRequest;
  $('context-list').replaceChildren(node('p', project ? '正在读取已保存上下文…' : '选择项目后查看已保存上下文。', 'help'));
  if (!project || !state.vaultId) return;
  try {
    const result = await request(vaultPath('/contexts') + '?' + new URLSearchParams({project}));
    if (requestId !== contextState.listRequest || generation !== state.generation || project !== $('search-project').value) return;
    $('context-list').replaceChildren();
    if (!result.contexts.length) $('context-list').append(node('p', '当前项目尚未保存上下文文件。', 'help'));
    for (const item of result.contexts) {
      const card = node('article', undefined, 'knowledge-card');
      card.append(node('h3', item.query), node('p', `${item.createdAt} · 知识 v${item.knowledgeVersion} · ${item.stale ? `已失效：${contextStaleText(item.staleReasons)}，请重新准备` : '与当前知识一致（本次读取时）'}`, 'help'), node('pre', item.path, 'code-block'));
      card.append(pathCopyControls(item.path)); $('context-list').append(card);
    }
  } catch (error) {
    if (requestId === contextState.listRequest && generation === state.generation) $('context-list').replaceChildren(node('p', `已保存列表读取失败：${errorMessage(error)} 已取得的保存回执仍保留。`, 'scope-note'));
  }
}

function evidenceScope() {
  return {vault: state.vaultId, project: $('search-project').value, query: $('evidence-query').value, maxBytes: Number($('evidence-budget').value)};
}
function evidenceScopeKey() { return JSON.stringify(evidenceScope()); }
function clearEvidenceReceipt() {
  evidenceState.receipt = null;
  $('evidence-receipt').hidden = true;
  $('evidence-saved-path').textContent = '';
  $('evidence-copy-tools').replaceChildren();
}
function invalidateEvidence(message = '条件已变化，请重新准备 CSV 证据包。') {
  invalidateVerification();
  evidenceState.requestId += 1;
  evidenceState.loading = false;
  evidenceState.preview = null;
  $('evidence-preview').value = '';
  $('evidence-results').replaceChildren();
  $('evidence-warnings').replaceChildren();
  $('evidence-status').textContent = message;
  updateEvidenceControls();
}
function updateEvidenceControls() {
  const ready = connectionState.phase === 'ready' && !connectionState.reconnecting && Boolean(state.vaultId);
  const locked = evidenceState.saving || Boolean(evidenceState.pending);
  $('evidence-prepare').disabled = !ready || locked || evidenceState.loading || workState.busy;
  $('evidence-save').disabled = !ready || evidenceState.saving || workState.busy || (!evidenceState.pending && !evidenceState.preview?.result.saveAllowed);
  $('evidence-save').textContent = evidenceState.saving ? '正在保存…' : evidenceState.pending ? '确认上次 CSV 证据保存结果' : '保存 CSV 证据包';
  for (const id of ['evidence-query', 'evidence-budget']) $(id).disabled = locked;
  $('search-project').disabled = locked || contextState.saving || Boolean(contextState.pending);
  if (evidenceState.pending) $('evidence-status').textContent = `上次 CSV 保存结果尚未确认（项目：${evidenceState.pending.body.project}）。重新连接后显式确认，沿用原事件与快照，不自动重发。`;
}
function evidenceStaleText(reasons) {
  const labels = {'source-scope-changed': '最新 CSV 来源范围已变化', 'parser-version-changed': '解析器版本已变化', 'algorithm-changed': '检索算法已变化'};
  return (reasons || []).map(code => labels[code] || code).join('；');
}
async function prepareEvidence(event) {
  event?.preventDefault();
  if (evidenceState.saving || evidenceState.pending || workState.busy) return;
  const scope = evidenceScope();
  if (!scope.project || !scope.query.trim() || [...scope.query].length > 200 || /[\u0000-\u001f\u007f]/.test(scope.query)) { showNotice('请选择项目，填写 1 至 200 字且不含控制字符的 CSV 关键词。', true); return; }
  invalidateEvidence('正在扫描最新 CSV 来源并准备证据…');
  const requestId = evidenceState.requestId, generation = state.generation, key = evidenceScopeKey();
  evidenceState.loading = true;
  updateEvidenceControls();
  try {
    const result = await request(vaultPath('/csv-evidence') + '?' + new URLSearchParams({project: scope.project, q: scope.query, maxBytes: scope.maxBytes}));
    if (generation !== state.generation || requestId !== evidenceState.requestId || key !== evidenceScopeKey()) return;
    evidenceState.preview = {result, key};
    $('evidence-preview').value = result.markdown;
    const c = result.counts;
    $('evidence-status').textContent = `${scope.project} · ${result.status === 'blocked' ? '扫描被限额阻断，匹配数未知，不能保存' : result.status === 'partial' ? '部分来源扫描失败，计数仅覆盖成功部分' : '来源扫描完成'} · 范围 ${c.scopeSourceCount} 个 CSV / ${c.scopeSourceBytes} 字节 · 已扫描 ${c.scannedSourceCount} · 失败 ${c.failedSourceCount} · 匹配 ${c.matchedRecordCount ?? '未知'} · 返回 ${c.returnedRecordCount} · 返回上限排除 ${c.returnLimitExcludedCount ?? '未知'} · 包内纳入 ${result.includedRecordCount} · 预算排除 ${result.budgetExcludedCount} · ${result.bytes} / ${result.maxBytes} 字节`;
    $('evidence-warnings').append(node('p', result.notice, 'scope-note'));
    for (const error of result.errors) $('evidence-warnings').append(node('p', `${error.code}${error.limit ? ` · ${error.limit}` : ''}${error.recordId ? ` · 来源 ${error.recordId} v${error.sourceVersion}` : ''}`, 'scope-note'));
    for (const entry of result.results) {
      const card = node('article', undefined, 'knowledge-card');
      const included = result.selectedRecords.some(item => item.recordId === entry.recordId && item.recordNumber === entry.recordNumber);
      card.append(node('h3', `来源 v${entry.sourceVersion} · 记录 ${entry.recordNumber} · ${included ? '纳入证据包' : '预算排除'}`), node('p', `${entry.path} · L${entry.lineStart}–L${entry.lineEnd} · 命中列 ${entry.matchedColumns.join('、')} · ${entry.parserVersion}`, 'help'), node('p', `SHA256：${entry.sourceSHA}`, 'help mono'), node('h4', '解码值'), node('pre', JSON.stringify(entry.cells), 'code-block'), node('h4', '原始CSV片段（完整记录物理行）'), node('pre', entry.rawExcerpt, 'code-block'));
      const open = node('button', '打开此来源版本原文', 'button');
      open.type = 'button';
      open.addEventListener('click', () => {
        if (generation !== state.generation || key !== evidenceScopeKey()) return;
        loadRecord(entry.recordId, entry.sourceVersion, {switchPage: true}).catch(error => showNotice(errorMessage(error), true));
      });
      card.append(open); $('evidence-results').append(card);
    }
  } catch (error) {
    if (generation === state.generation && requestId === evidenceState.requestId && key === evidenceScopeKey()) { $('evidence-status').textContent = '未取得有效 CSV 证据预览，请核对后重新准备。'; showNotice(errorMessage(error), true); }
  } finally {
    if (requestId === evidenceState.requestId) { evidenceState.loading = false; updateEvidenceControls(); }
  }
}
async function saveEvidence() {
  if (evidenceState.saving || workState.busy) return;
  if (!evidenceState.pending) {
    const preview = evidenceState.preview;
    if (!preview?.result.saveAllowed || preview.key !== evidenceScopeKey()) { invalidateEvidence(); return; }
    const scope = evidenceScope();
    evidenceState.pending = {endpoint: vaultPath('/csv-evidence'), vault: scope.vault, body: {eventId: `csv-evidence-ui-${crypto.randomUUID()}`, project: scope.project, query: scope.query, maxBytes: scope.maxBytes, expectedSnapshotId: preview.result.snapshotId}};
  }
  const pending = evidenceState.pending;
  if (pending.vault !== state.vaultId) { showNotice('上次 CSV 保存属于其他库，请恢复原库后确认。', true); return; }
  $('evidence-event').textContent = pending.body.eventId;
  evidenceState.saving = true;
  updateWorkControls();
  $('vault-select').disabled = true;
  try {
    const result = await request(pending.endpoint, {method: 'POST', body: JSON.stringify(pending.body)});
    if (evidenceState.pending !== pending || pending.vault !== state.vaultId) return;
    evidenceState.receipt = result;
    evidenceState.pending = null;
    $('evidence-receipt').hidden = false;
    $('evidence-saved-path').textContent = result.path;
    $('evidence-copy-tools').replaceChildren(pathCopyControls(result.path, '复制 CSV 证据路径'));
    $('evidence-saved-status').textContent = `${result.duplicate ? '已确认此前保存' : '已保存 CSV 来源证据文件'} · ${pending.body.project}${result.stale ? `；已过期：${evidenceStaleText(result.staleReasons)}` : '；保存的是当时的来源快照，未经事实确认。'}`;
    invalidateEvidence('CSV 证据已保存，再次保存前请重新准备。');
    await refreshEvidenceList();
  } catch (error) {
    // Only these responses establish that this exact request did not commit.
    // Auth/integrity failures can occur before the server checks a prior event.
    if (['EVIDENCE_STALE', 'EVIDENCE_SCAN_BLOCKED', 'EVENT_CONFLICT'].includes(error.code)) {
      evidenceState.pending = null;
      invalidateEvidence(error.code === 'EVIDENCE_STALE' ? 'CSV 来源范围或预览已变化，旧快照不能保存，请重新准备。' : '服务拒绝本次 CSV 保存，请核对后重新准备。');
    }
    showNotice(errorMessage(error), true);
  } finally {
    evidenceState.saving = false;
    updateWorkControls();
    $('vault-select').disabled = workState.busy || modelState.busy || contextState.saving;
  }
}
async function refreshEvidenceList() {
  invalidateVerification();
  const project = $('search-project').value, generation = state.generation, requestId = ++evidenceState.listRequest;
  const vaultId = state.vaultId;
  const vaultIdentity = state.vaults.find(vault => vault.id === vaultId);
  const expectedVaultId = vaultIdentity?.vaultId, expectedPath = vaultIdentity?.path;
  $('evidence-list').replaceChildren(node('p', project ? '正在读取 CSV 证据包…' : '选择项目后查看 CSV 证据包。', 'help'));
  if (!project || !state.vaultId) return;
  try {
    const result = await request(vaultPath('/csv-evidence') + '?' + new URLSearchParams({project}));
    if (requestId !== evidenceState.listRequest || generation !== state.generation || project !== $('search-project').value) return;
    $('evidence-list').replaceChildren();
    if (!result.packages.length) $('evidence-list').append(node('p', '当前项目尚未保存 CSV 证据包。', 'help'));
    for (const item of result.packages) {
      const card = node('article', undefined, 'knowledge-card');
      card.append(node('h3', item.query), node('p', `${item.createdAt} · ${item.status} · ${item.includedRecordCount} 条纳入 · ${item.bytes} 字节 · ${item.stale ? `已过期：${evidenceStaleText(item.staleReasons)}` : 'CSV 来源范围一致（本次读取时），未经事实确认'}`, 'help'), node('pre', item.path, 'code-block'), pathCopyControls(item.path));
      const verify = node('button', '核验复用', 'button');
      verify.type = 'button';
      verify.addEventListener('click', () => {
        // A verified reconnect changes generation, but this package still belongs
        // to the same displayed list and Vault. Validate its durable scope here;
        // verifyEvidence captures the new generation for the asynchronous result.
        const currentVault = state.vaults.find(vault => vault.id === vaultId);
        if (requestId !== evidenceState.listRequest || vaultId !== state.vaultId || project !== $('search-project').value
            || !expectedVaultId || !expectedPath || currentVault?.vaultId !== expectedVaultId || currentVault?.path !== expectedPath) return;
        return verifyEvidence(item.id);
      });
      card.append(verify);
      $('evidence-list').append(card);
    }
  } catch (error) {
    if (requestId === evidenceState.listRequest && generation === state.generation && project === $('search-project').value) $('evidence-list').replaceChildren(node('p', `CSV 证据包读取失败：${errorMessage(error)} 保存回执仍保留。`, 'scope-note'));
  }
}

function invalidateVerification(message = '从已保存 CSV 包选择“核验复用”。核验只代表检查时刻。') {
  verificationState.requestId += 1;
  verificationState.packageId = null;
  verificationState.result = null;
  $('verification-status').textContent = message;
  $('verification-report').replaceChildren();
  $('verification-body').value = '';
  $('verification-body-label').hidden = true;
}
function renderVerification(report) {
  const labels = {usable: 'usable · 本次复用核验通过', stale: 'stale · 原包已过期，不能作为本次核验通过的证据', blocked: 'blocked · 扫描范围超限，尚未完成核验'};
  if (!Object.prototype.hasOwnProperty.call(labels, report.currentReuseStatus)) throw new Error('服务未返回有效核验状态。');
  verificationState.result = report;
  $('verification-status').textContent = `${labels[report.currentReuseStatus]}。核验只代表检查时刻。`;
  const container = $('verification-report');
  container.append(node('p', `原包扫描状态：${report.packageStatus}；这是保存时的状态，与本次复用核验状态分开。`, 'scope-note'), node('p', report.notice, 'scope-note'));
  if (report.currentReuseStatus === 'stale') container.append(node('p', `过期原因：${evidenceStaleText(report.staleReasons)}。未交付正文。`, 'scope-note'));
  if (report.currentReuseStatus === 'blocked') container.append(node('p', '当前 CSV 范围超过 32 个来源或 1,048,576 字节；尚未完成核验，是否过期未知。本次未读取当前 CSV 正文，不表示整个服务没有内部读取。', 'scope-note'));
  const c = report.packageCounts, l = report.ledger;
  container.append(node('h3', '原包查询限制（保存时）'), node('p', `来源 ${c.scopeSourceCount} / ${c.scopeSourceBytes} 字节；扫描 ${c.scannedSourceCount}；失败 ${c.failedSourceCount}；匹配 ${c.matchedRecordCount}；返回 ${c.returnedRecordCount}；返回上限排除 ${c.returnLimitExcludedCount}；原包纳入 ${c.savedIncludedRecordCount}；预算排除 ${c.savedBudgetExcludedCount}。这些不是新查询统计；无匹配不能推出业务事实不存在。`, 'help'));
  container.append(node('h3', '本次显式读取账本'));
  const fields = [['recordBytes', '包登记字节'], ['markdownBytes', '包正文的字节'], ['currentScopeSourceCount', '当前范围来源数'], ['currentScopeSourceBytes', '当前范围源字节'], ['currentSourceBytesRead', '当前源正文已读字节'], ['currentSourceFilesRead', '当前源已读文件数'], ['uniqueFilesRead', '本账本去重文件数'], ['logicalReferencesChecked', '已核对逻辑引用数'], ['uniqueReferencedSourceFiles', '引用的不同源文件数'], ['duplicateReferenceCount', '重复引用数'], ['measuredReadBytes', '显式读取字节合计']];
  const table = node('table', undefined, 'csv-table');
  for (const [key, label] of fields) { const row = node('tr'); row.append(node('th', label), node('td', l[key] === null ? '未核对' : String(l[key]))); table.append(row); }
  container.append(table, node('p', l.unmeasuredInternalIO, 'help'), node('p', '账本仅计本次显式逻辑读取，不是总 I/O、token 或全部复用成本。', 'scope-note'));
  const details = node('details'); details.append(node('summary', '核验身份与签名'), node('pre', JSON.stringify({project: report.project, packageId: report.packageId, path: report.path, snapshotId: report.snapshotId, sourceSignature: report.sourceSignature, currentSourceSignature: report.currentSourceSignature, markdownSHA: report.markdownSHA}, null, 2), 'code-block')); container.append(details);
  if (report.currentReuseStatus === 'usable' && typeof report.markdown === 'string') {
    $('verification-body').value = report.markdown;
    $('verification-body-label').hidden = false;
  }
}
async function verifyEvidence(packageId) {
  invalidateVerification('正在核验所选包与当前 CSV 原始字节，尚未取得结果…');
  verificationState.packageId = packageId;
  const project = $('search-project').value, vault = state.vaultId, generation = state.generation, requestId = verificationState.requestId;
  const current = () => requestId === verificationState.requestId && packageId === verificationState.packageId && generation === state.generation && vault === state.vaultId && project === $('search-project').value;
  try {
    const result = await request(vaultPath('/csv-evidence-verify') + '?' + new URLSearchParams({project, packageId, includeMarkdown: '1'}), {}, current);
    if (!current()) return;
    if (result.currentReuseStatus === 'blocked') throw new Error('阻断报告的 HTTP 状态不符合核验接口。');
    renderVerification(result);
  } catch (error) {
    if (!current()) return;
    if (error.httpStatus === 409 && error.responseBody?.kind === 'csv-source-evidence-verification' && error.responseBody.currentReuseStatus === 'blocked') {
      renderVerification(error.responseBody);
      return;
    }
    verificationState.result = null;
    $('verification-report').replaceChildren();
    $('verification-body').value = '';
    $('verification-body-label').hidden = true;
    $('verification-status').textContent = `failed · 本次核验失败，当前可复用性未知；未交付正文。${errorMessage(error)} 未自动重新核验或保存。`;
  }
}

function newEvent() {
  $('import-event').value = `ui-${crypto.randomUUID()}`;
  $('import-result').hidden = true;
}

function selectedMode() { return document.querySelector('input[name="content-mode"]:checked').value; }

function updateContentMode() {
  const isFile = selectedMode() === 'file';
  $('text-input-panel').hidden = isFile;
  $('file-input-panel').hidden = !isFile;
  $('import-content').required = !isFile;
  $('import-file').required = isFile;
}

function base64FromBytes(bytes) {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  return btoa(binary);
}

async function importRecord(event) {
  event.preventDefault();
  const dataScope = currentDataScope();
  if (!$('synthetic-confirm').checked) { showNotice(isHumanTrial() ? '请先确认本次提交是低风险真实试用资料，并同意保存到本机试用库。' : '请先确认本次提交只包含合成测试资料。', true); return; }
  const generation = state.generation;
  const vaultId = state.vaultId;
  const payload = {
    eventId: $('import-event').value,
    project: $('import-project').value.trim(),
    source: {id: $('import-source-id').value.trim(), tool: $('import-source-tool').value.trim(), locator: $('import-locator').value.trim(), recordedAt: null, sessionId: null},
    filename: $('import-filename').value.trim(),
    expectedVersion: Number($('import-version').value),
    kind: $('import-kind').value,
    dataScope,
    synthetic: dataScope === 'synthetic',
  };
  if (dataScope === 'human-trial') payload.humanTrial = true;
  $('import-submit').disabled = true;
  $('new-event-button').disabled = true;
  $('import-result').hidden = true;
  try {
    if (!vaultId) throw new Error('请先连接本地服务并选择记忆库。');
    const endpoint = `/api/vaults/${encodeURIComponent(vaultId)}/imports`;
    if (!payload.project || !payload.source.id || !payload.eventId) throw new Error('项目、来源 ID 和事件 ID 不能为空。');
    if (/[\\/]/.test(payload.filename) || ['.', '..'].includes(payload.filename)) throw new Error('文件名必须是单个名称，不能包含目录或路径。');
    if (!Number.isSafeInteger(payload.expectedVersion) || payload.expectedVersion < 0) throw new Error('预期已有版本号必须是非负整数。');
    if (selectedMode() === 'file') {
      const file = $('import-file').files[0];
      if (!file) throw new Error('请先选择资料文件。');
      if (file.size > 2 * 1024 * 1024) throw new Error('单个文件不能超过 2 MiB。');
      payload.contentBase64 = base64FromBytes(new Uint8Array(await file.arrayBuffer()));
    } else {
      payload.content = $('import-content').value;
      if (new TextEncoder().encode(payload.content).length > 2 * 1024 * 1024) throw new Error('文本内容不能超过 2 MiB。');
    }
    const result = await request(endpoint, {method: 'POST', body: JSON.stringify(payload)});
    if (generation !== state.generation) return;
    navigationState.importDirty = false;
    const record = result.record;
    $('import-result').textContent = `${result.duplicate ? '同一事件已存在，返回原记录；没有重复入库。' : '已真实保存。'}\n${record.filename} · v${record.version}\n记录 ID：${record.id}\n文件：${record.path}\n解析：${parseLabels[record.parseStatus] || record.parseStatus}\n事件 ID：${payload.eventId}`;
    $('import-result').hidden = false;
    showNotice(result.duplicate ? '重复事件已确认，没有新增原始记录。' : `${scopeLabel()}已保存，事件 ID 已保留，可用于确认或重复提交。`);
    await refreshVault();
  } catch (error) { if (generation === state.generation) showNotice(errorMessage(error), true); }
  finally {
    $('import-submit').disabled = false;
    $('new-event-button').disabled = false;
  }
}

async function reindex() {
  const button = $('reindex-button');
  const generation = state.generation;
  button.disabled = true;
  try {
    const result = await request(vaultPath('/reindex'), {method: 'POST', body: '{}'});
    if (generation !== state.generation) return;
    await refreshVault();
    showNotice(`基础索引已重建，服务返回 ${Array.isArray(result.records) ? result.records.length : result.records} 条版本记录。`);
  } catch (error) { if (generation === state.generation) showNotice(errorMessage(error), true); }
  finally { button.disabled = false; }
}

function selectedModel() { return modelState.profiles.find((profile) => profile.id === modelState.selectedId) || null; }

function modelPath(suffix) {
  if (!selectedModel()) throw new Error('请先保存模型连接。');
  return `/api/models/${encodeURIComponent(modelState.selectedId)}/${suffix}`;
}

function formatYuan(micros) { return Number.isSafeInteger(micros) ? (micros / 1000000).toFixed(6).replace(/\.?0+$/, '') || '0' : '未知'; }

function moneyField(id, allowUnknown) {
  const raw = $(id).value.trim();
  if (!raw && allowUnknown) return 0;
  if (!/^\d+(?:\.\d{1,6})?$/.test(raw)) throw new Error('费用上限与单价须为非负金额，最多保留六位小数；已确认费率时不能为空。');
  const [whole, decimal = ''] = raw.split('.');
  const micros = Number(whole) * 1000000 + Number(decimal.padEnd(6, '0'));
  if (!Number.isSafeInteger(micros)) throw new Error('金额过大，请填写可精确计算的费用上限。');
  return micros;
}

function updateModelKind() {
  const external = $('model-kind').value === 'external';
  const inputBytes = selectedModel()?.limits?.maxInputBytes ?? 16384;
  $('model-budget-help').textContent = `当前单次输入上限 ${inputBytes} 字节（${Number((inputBytes / 1024).toFixed(2))} KiB），保存编辑时保留此值。供应商可能另计或超出输出用量，请求上限不是账户硬限；报告用量另行核对。外部服务费率未知时不能发起检查，修改上限不会补回已用额度。`;
  $('model-kind-note').textContent = external
    ? '此连接可能向第三方发送文本。即使 Base URL 是 127.0.0.1 或 localhost，网关仍可能转发到云端；选择连接不等于授权外发。'
    : '本地标签和回环地址不能证明推理未外发。服务必须取得本地执行证据后才允许检查；本地不可用不会静默切换云端。';
  $('model-rates-label').hidden = !external;
  $('model-rates-note').textContent = external ? '单价需由你依据实际服务费率确认；服务按填写的人民币费率执行预算。' : '本地推理按无第三方 API 账单登记为 0 元；这不代表没有设备与电力成本。';
  for (const id of ['model-max-cost', 'model-input-price', 'model-output-price']) {
    $(id).disabled = modelState.busy || !external;
    $(id).required = external && $('model-rates-known').checked;
  }
}

function updateModelControls() {
  const profile = selectedModel();
  const unavailable = connectionState.phase !== 'ready' || connectionState.reconnecting || modelState.busy || modelState.dirty || !profile;
  $('model-save').disabled = connectionState.phase !== 'ready' || connectionState.reconnecting || modelState.busy;
  const localBlocked = profile?.kind === 'local' && profile?.locality?.status !== 'verified';
  const ratesBlocked = profile?.kind === 'external' && profile?.limits?.ratesKnown !== true;
  const authorized = profile?.authorization?.scope === 'synthetic-probe';
  $('model-save-credential').disabled = unavailable || !modelState.credentialStore.available;
  $('model-secret').disabled = unavailable || !modelState.credentialStore.available;
  $('model-authorize').disabled = unavailable || !$('model-authorize-confirm').checked;
  $('model-authorize-confirm').disabled = unavailable;
  $('model-check').disabled = unavailable || localBlocked || ratesBlocked || !authorized;
  $('model-primary').disabled = unavailable || localBlocked || ratesBlocked || !authorized || profile?.capabilities?.text !== 'passed' || modelState.primary === profile?.id;
  $('model-primary').textContent = profile && modelState.primary === profile.id ? '当前管家主模型' : '设为管家主模型';
  $('model-check-help').textContent = modelState.busy ? '正在等待本机服务处理，请勿重复提交。' : modelState.dirty ? '配置有未保存更改，请先保存；保存后验证与授权会失效。' : localBlocked ? '检查阻塞：本地执行路径尚未验证，不能仅按本地地址认定数据不会外发。' : ratesBlocked ? '检查阻塞：第三方费率尚未确认，请核实单价与费用上限后保存。' : !authorized ? '请先主动勾选并保存固定合成探针授权。' : '检查只在点击后发起。文本、结构化输出与工具能力独立记录；网络成功不表示三项能力均通过。';
  updateModelKind();
  renderProjectGrants();
  updateReconnectControl();
}

function renderModelList() {
  $('model-list').replaceChildren();
  if (!modelState.profiles.length) $('model-list').append(node('div', '尚未配置模型服务。保存连接后，授权并检查实际能力。', 'empty'));
  for (const profile of modelState.profiles) {
    const button = node('button', undefined, 'record-row');
    button.type = 'button';
    button.classList.toggle('selected', profile.id === modelState.selectedId);
    button.disabled = modelState.busy;
    button.append(node('b', profile.name), node('small', `${profile.kind === 'local' ? '本地' : '第三方 / 网关'} · ${profile.model}`));
    if (profile.id === modelState.primary) button.append(node('span', '管家主模型', 'tag green'));
    button.addEventListener('click', () => selectModel(profile.id));
    $('model-list').append(button);
  }
  const backend = modelState.credentialStore.backend || '尚未提供';
  $('models-status').textContent = `${modelState.profiles.length} 个连接 · 凭据存储：${backend}${modelState.credentialStore.available ? '可用' : '不可用'} · ${modelState.primary ? '已选管家主模型' : '尚未选择主模型'}`;
}

function renderSavedModel() {
  const profile = selectedModel();
  $('model-saved-panel').hidden = !profile;
  if (!profile) { updateModelControls(); return; }
  const metadata = $('model-metadata');
  metadata.replaceChildren();
  addMetadata(metadata, '配置版本', `r${profile.revision}`);
  addMetadata(metadata, '服务地址', profile.baseUrl);
  addMetadata(metadata, '适配器', ({csglite: 'CSGLite', opencsg: 'OpenCSG 直连 · Qwen 关闭思考', generic: '通用 OpenAI 兼容'})[profile.adapter] || profile.adapter);
  addMetadata(metadata, '配置目录', modelState.runtimePath);
  addMetadata(metadata, '已用额度', `${profile.usage?.calls ?? 0} / ${profile.limits?.maxCalls ?? '未知'} 次 · 预留费用 ${formatYuan(profile.usage?.reservedCostMicros)} / ${formatYuan(profile.limits?.maxCostMicros)} 元`);
  const capabilities = $('model-capabilities');
  capabilities.replaceChildren();
  const statuses = {passed: '已通过', failed: '失败', unverified: '未验证'};
  for (const [key, title] of [['text', '文本推理'], ['structured', '结构化输出'], ['tools', '工具调用格式']]) {
    const status = profile.capabilities?.[key] || 'unverified';
    const card = node('div', undefined, 'capability-card');
    card.append(node('span', title), node('strong', statuses[status] || String(status), status === 'passed' ? 'capability-pass' : status === 'failed' ? 'capability-fail' : ''));
    capabilities.append(card);
  }
  $('model-locality').textContent = profile.kind === 'external' ? '外发边界：此连接按第三方服务处理；请求到本机网关仍可能转到云端。' : `本地执行：${profile.locality?.status === 'verified' ? '已取得证据' : '未验证'}。${profile.locality?.detail || '尚无可复核的本地执行证据。'}`;
  $('model-credential-status').textContent = profile.credentialPresent ? `已保存凭据引用：${profile.credentialRef || '由系统保管'}。页面不会读取或回显密钥。` : '此连接尚未保存凭据。无需鉴权的服务可以直接进行授权。';
  if (!modelState.credentialStore.available) $('model-credential-status').textContent += ' 当前安全凭据存储不可用，不能保存 API Key。';
  $('model-authorization-status').textContent = profile.authorization?.scope === 'synthetic-probe' ? `当前配置 r${profile.revision} 已获固定合成探针授权。` : '当前配置尚未获得探针授权。';
  updateModelControls();
}

function selectModel(id) {
  modelState.selectedId = id;
  modelState.dirty = false;
  const profile = selectedModel();
  const limits = profile?.limits || {};
  $('model-editor-title').textContent = profile ? `编辑连接 · ${profile.name}` : '新建模型连接';
  $('model-name').value = profile?.name || '';
  $('model-kind').value = profile?.kind || 'local';
  $('model-adapter').value = profile?.adapter || 'generic';
  $('model-base-url').value = profile?.baseUrl || '';
  $('model-model').value = profile?.model || '';
  $('model-max-calls').value = limits.maxCalls ?? 8;
  $('model-max-seconds').value = limits.maxSeconds ?? 30;
  $('model-max-output').value = limits.maxOutputTokens ?? 256;
  const known = profile?.kind === 'external' ? limits.ratesKnown === true : true;
  $('model-rates-known').checked = profile?.kind === 'external' && known;
  $('model-max-cost').value = known ? formatYuan(limits.maxCostMicros ?? 0) : '';
  $('model-input-price').value = known ? formatYuan(limits.inputMicrosPerMillion ?? 0) : '';
  $('model-output-price').value = known ? formatYuan(limits.outputMicrosPerMillion ?? 0) : '';
  $('model-secret').value = '';
  $('model-authorize-confirm').checked = false;
  $('model-project-confirm').checked = false;
  $('model-check-result').hidden = !profile?.lastCheck;
  $('model-check-result').textContent = profile?.lastCheck ? JSON.stringify(profile.lastCheck, null, 2) : '';
  renderModelList();
  renderSavedModel();
}

async function refreshModels({selectId = modelState.selectedId} = {}) {
  const result = await request('/api/models');
  modelState.profiles = result.profiles || [];
  modelState.primary = result.primary || null;
  modelState.credentialStore = result.credentialStore || {};
  modelState.runtimePath = result.runtimePath || null;
  modelState.loaded = true;
  selectModel(modelState.profiles.some((profile) => profile.id === selectId) ? selectId : modelState.profiles[0]?.id || null);
  renderWorkModels();
}

async function runModelAction(action) {
  if (modelState.busy) return;
  modelState.busy = true;
  for (const control of $('page-models').querySelectorAll('button,input,select')) control.disabled = true;
  updateModelControls();
  try { await action(); }
  catch (error) {
    showNotice(errorMessage(error), true);
    $('model-check-result').textContent = errorMessage(error);
    $('model-check-result').hidden = false;
  } finally {
    modelState.busy = false;
    for (const control of $('page-models').querySelectorAll('button,input,select')) control.disabled = false;
    renderModelList();
    renderSavedModel();
  }
}

async function saveModelProfile(event) {
  event.preventDefault();
  await runModelAction(async () => {
    const profile = selectedModel();
    const external = $('model-kind').value === 'external';
    const ratesKnown = !external || $('model-rates-known').checked;
    const payload = {
      name: $('model-name').value.trim(), kind: $('model-kind').value, adapter: $('model-adapter').value,
      baseUrl: $('model-base-url').value.trim(), model: $('model-model').value.trim(), expectedRevision: profile?.revision ?? 0,
      limits: {maxCalls: Number($('model-max-calls').value), maxSeconds: Number($('model-max-seconds').value), maxOutputTokens: Number($('model-max-output').value), maxInputBytes: profile?.limits?.maxInputBytes ?? 16384, currency: 'CNY', ratesKnown,
        maxCostMicros: external ? moneyField('model-max-cost', !ratesKnown) : 0,
        inputMicrosPerMillion: external ? moneyField('model-input-price', !ratesKnown) : 0,
        outputMicrosPerMillion: external ? moneyField('model-output-price', !ratesKnown) : 0},
    };
    if (profile) payload.id = profile.id;
    const result = await request('/api/models/profiles', {method: 'POST', body: JSON.stringify(payload)});
    await refreshModels({selectId: result.profile.id});
    showNotice('连接配置已保存。请按当前配置重新授权并检查能力。');
  });
}

async function saveModelCredential(event) {
  event.preventDefault();
  const secret = $('model-secret').value;
  $('model-secret').value = '';
  if (!secret) { showNotice('请先在安全凭据输入框填写 API Key。', true); return; }
  await runModelAction(async () => {
    await request(modelPath('credential'), {method: 'POST', body: JSON.stringify({secret, expectedRevision: selectedModel().revision})});
    await refreshModels();
    showNotice('凭据已保存到 macOS Keychain，输入框已清空。');
  });
}

async function authorizeModel() {
  if (!$('model-authorize-confirm').checked) return;
  await runModelAction(async () => {
    await request(modelPath('authorize'), {method: 'POST', body: JSON.stringify({expectedRevision: selectedModel().revision, scope: 'synthetic-probe', allow: true})});
    await refreshModels();
    showNotice('当前配置的固定合成能力探针授权已保存；尚未调用模型。');
  });
}

async function checkModel() {
  await runModelAction(async () => {
    $('model-check-result').textContent = '正在执行实际能力检查，等待服务返回。可能需要约 30–60 秒；未返回前不记为通过。';
    $('model-check-result').hidden = false;
    const result = await request(modelPath('check'), {method: 'POST', body: JSON.stringify({expectedRevision: selectedModel().revision})});
    if (result.profile) modelState.profiles = modelState.profiles.map((profile) => profile.id === result.profile.id ? result.profile : profile);
    $('model-check-result').textContent = JSON.stringify(result.checks || [], null, 2);
    const capabilities = result.profile?.capabilities || {};
    const passed = ['text', 'structured', 'tools'].filter((key) => capabilities[key] === 'passed').length;
    showNotice(`检查已返回：${passed} / 3 项能力通过。请查看逐项结果与未通过原因。`, passed !== 3);
  });
}

function jobLabel(value) {
  return ({queued: '等待处理', pending: '等待处理', running: '处理中', processing: '处理中', validating: '验证中', publishing: '留存中', 'candidate-ready': '候选已形成', committed: '已验证并留存', 'no-work': '没有新内容', 'needs-decision': '需要处理', completed: '已完成', succeeded: '已完成', failed: '失败', blocked: '阻塞', interrupted: '已中断', cancelled: '已取消', stale: '资料已变化', paused: '已暂停', idle: '空闲', current: '当前可用', 'pending-review': '待复核', historical: '历史版本'})[value] || value || '未提供状态';
}

function freshWorkEvent(kind) { return `${kind}-${crypto.randomUUID()}`; }
function currentKnowledgeVersion() { return workState.knowledge?.current?.version ?? 0; }
function stopWorkPoll() { clearTimeout(workState.poll); workState.poll = null; }
function newQueueEvent() { workState.queueEvent = freshWorkEvent('steward-ui'); workState.queuePayload = null; $('steward-event').textContent = workState.queueEvent; }

function resetWorkScope() {
  stopWorkPoll();
  workState.refreshId += 1;
  workState.knowledgeRequest += 1;
  workState.searchRequest += 1;
  Object.assign(workState, {project: '', status: null, unconfigured: false, knowledge: null, knowledgeLoad: 'idle', knowledgeError: '', historical: null, correction: null, restore: null, statusFingerprint: null});
  newQueueEvent();
  for (const id of ['steward-project', 'knowledge-project', 'model-project-scope']) fillProjects(id, [], projectPlaceholder());
  $('steward-status').textContent = '正在读取后台状态…';
  $('steward-jobs').replaceChildren(node('div', '正在读取当前记忆库队列…', 'empty'));
  $('steward-queue-count').textContent = '尚未读取';
  $('steward-round').replaceChildren();
  $('correction-text').value = '';
  $('knowledge-restore-reason').value = '';
  clearKnowledgeSearch();
  renderKnowledge();
}

function workModel() { return modelState.profiles.find((profile) => profile.id === $('steward-model').value) || null; }
function grants(profile) { return Array.isArray(profile?.projectAuthorizations) ? profile.projectAuthorizations : []; }
function validProjectGrant(profile, project, policy) {
  const stableId = state.vaults.find((vault) => vault.id === state.vaultId)?.vaultId;
  const dataScope = currentDataScope();
  return grants(profile).some((grant) => [state.vaultId, stableId].filter(Boolean).includes(grant.vaultId)
    && grant.project === project && grant.dataPolicy === policy && (grant.dataScope || 'synthetic') === dataScope
    && grant.revision === profile.revision && grant.allow !== false);
}

function renderWorkModels() {
  const selected = $('steward-model').value;
  $('steward-model').replaceChildren(node('option', workState.unconfigured ? '管家未配置（当前记忆库已选择）' : '请选择管家模型'));
  if (workState.unconfigured) { $('steward-model').firstElementChild.value = ''; updateWorkControls(); return; }
  $('steward-model').firstElementChild.value = '';
  for (const profile of modelState.profiles) {
    const option = node('option', `${profile.name} · ${profile.kind === 'external' ? '第三方' : '本地'}${modelState.primary === profile.id ? ' · 主模型' : ''}`);
    option.value = profile.id;
    $('steward-model').append(option);
  }
  if (modelState.profiles.some((profile) => profile.id === selected)) $('steward-model').value = selected;
  else if (modelState.primary) $('steward-model').value = modelState.primary;
  updateWorkControls();
}

function updateWorkControls() {
  const vaultReady = Boolean(!contextState.saving && !evidenceState.saving && connectionState.phase === 'ready' && !connectionState.reconnecting && state.vaultId && state.status && state.vaults.some((vault) => vault.id === state.vaultId));
  const profile = workModel();
  const policy = $('steward-policy').value;
  const scope = workState.project;
  const authorized = profile && validProjectGrant(profile, scope, policy);
  const incompatible = profile?.kind === 'external' && policy === 'local-only';
  $('steward-scope').textContent = workState.unconfigured ? '该服务未启用管家运行目录；本轮不能加入队列或运行。可查看已有原文与知识。' : !scope ? `请先选择一个${isHumanTrial() ? '试用' : '合成'}项目。` : !profile ? '请先选择已配置的管家模型。' : incompatible ? '所选模型是第三方服务，不能用于“仅本地处理”。请选择本地模型，或主动选择已授权的第三方处理范围。' : `${profile.name} · r${profile.revision} · ${scope} · ${scopeLabel()} · ${policy === 'local-only' ? '仅本地处理' : '允许第三方处理'}。${authorized ? '该范围已有有效项目授权。' : '该范围尚未授权，请到模型服务保存项目授权。'}`;
  const running = Boolean(workState.status?.running);
  const paused = Boolean(workState.status?.paused);
  $('steward-enqueue').disabled = workState.unconfigured || !vaultReady || workState.busy || !scope || !profile || !authorized || incompatible || !workState.knowledge || workState.knowledgeLoad !== 'ready';
  $('steward-run').disabled = !vaultReady || workState.busy || !workState.status || running || paused || !(workState.status.jobs || []).some((job) => ['queued', 'pending', 'candidate-ready'].includes(job.status));
  $('steward-pause').disabled = !vaultReady || workState.busy || !workState.status;
  $('steward-pause').textContent = paused ? '恢复处理许可' : '暂停后续处理';
  for (const id of ['steward-new-event', 'steward-max-jobs', 'steward-max-seconds']) $(id).disabled = !vaultReady || workState.busy || (id !== 'steward-new-event' && running);
  for (const id of ['steward-project', 'knowledge-project', 'steward-model', 'steward-policy']) $(id).disabled = !vaultReady || workState.busy;
  updateCorrectionGuard();
  updateRestoreGuard();
  updateContextControls();
  updateReconnectControl();
}

async function setWorkProject(project, {loadKnowledge = true} = {}) {
  if (project && !(state.status?.projects || []).includes(project)) throw new Error(`当前记忆库没有项目“${project}”；未切换到其他项目。`);
  workState.project = project;
  workState.knowledgeRequest += 1;
  workState.searchRequest += 1;
  Object.assign(workState, {knowledge: null, knowledgeLoad: project ? 'loading' : 'idle', knowledgeError: '', historical: null, correction: null, restore: null});
  $('steward-project').value = $('knowledge-project').value = project;
  $('correction-text').value = '';
  $('knowledge-restore-reason').value = '';
  newQueueEvent();
  clearKnowledgeSearch();
  renderKnowledge();
  syncNavigationLocation();
  if (loadKnowledge) await refreshKnowledge();
}

function renderSteward(result) {
  const jobs = Array.isArray(result.jobs) ? result.jobs : [];
  $('steward-status').textContent = connectionState.phase !== 'ready' ? '连接不可用，当前运行状态未知。下方保留上次读取的任务记录。' : `${result.running ? '本轮正在运行' : '当前没有运行中的轮次'} · ${result.paused ? '后续处理已暂停' : '后续处理许可已开启'}`;
  $('steward-status').className = `run-status ${result.running ? 'is-running' : ''}`;
  const queued = jobs.filter((job) => ['queued', 'pending', 'candidate-ready'].includes(job.status)).length;
  $('steward-queue-count').textContent = `${jobs.length} 个任务 · ${queued} 个待处理`;
  $('steward-round').replaceChildren();
  if (result.round) {
    const detail = node('details');
    detail.append(node('summary', '查看本轮实际记录'), node('pre', JSON.stringify(result.round, null, 2), 'code-block'));
    $('steward-round').append(detail);
  }
  const list = $('steward-jobs');
  list.replaceChildren();
  if (!jobs.length) list.append(node('div', '队列为空。先选择项目，将当前资料加入队列。', 'empty'));
  for (const job of [...jobs].reverse()) {
    const card = node('article', undefined, 'job-card');
    const head = node('div', undefined, 'job-heading');
    const status = job.status;
    head.append(node('h3', job.project || '项目未提供'), node('span', jobLabel(status), `tag ${['committed', 'no-work', 'completed', 'succeeded'].includes(status) ? 'green' : ['failed', 'blocked', 'interrupted', 'stale', 'needs-decision'].includes(status) ? 'amber' : 'blue'}`));
    card.append(head, node('p', `任务 ${job.id} · 第 ${job.attempt ?? 0} 次尝试`, 'help mono'));
    const info = [job.modelId ? `模型 ${job.modelId}` : '', job.dataPolicy ? (job.dataPolicy === 'local-only' ? '仅本地处理' : '已选第三方处理') : '', job.createdAt || ''].filter(Boolean).join(' · ');
    card.append(node('p', info, 'help'));
    if (job.error || job.reason) card.append(node('p', typeof (job.error || job.reason) === 'string' ? (job.error || job.reason) : `${job.error?.message || '处理异常'}${job.error?.code ? `（${job.error.code}）` : ''}`, 'job-error'));
    if (status === 'needs-decision') card.append(node('p', '此任务需要明确的决定或新证据。请补充合成原文后创建新任务；不能用自动重试代替判断。', 'help'));
    const actions = node('div', undefined, 'model-actions');
    const view = node('button', '查看项目知识', 'text-button');
    view.type = 'button';
    view.disabled = workState.busy;
    view.addEventListener('click', async () => { try { await setWorkProject(job.project); showPage('knowledge'); } catch (error) { showNotice(errorMessage(error), true); } });
    actions.append(view);
    if (['failed', 'interrupted'].includes(status) && (job.attempt ?? 0) < (job.maxAttempts ?? 2)) {
      const retry = node('button', '重试此任务', 'button');
      retry.type = 'button';
      retry.disabled = workState.busy || result.running;
      retry.addEventListener('click', () => runWorkAction(async () => {
        await request(vaultPath(`/steward/jobs/${encodeURIComponent(job.id)}/retry`), {method: 'POST', body: JSON.stringify({expectedAttempt: job.attempt ?? 0})});
        await refreshSteward();
        showNotice('重试申请已返回。请核对任务状态；待处理任务需主动开始新一轮。');
      }));
      actions.append(retry);
    }
    card.append(actions);
    const trace = node('details', undefined, 'job-trace');
    trace.append(node('summary', '处理轨迹与留存信息'), node('pre', JSON.stringify(job, null, 2), 'code-block'));
    card.append(trace);
    list.append(card);
  }
  updateWorkControls();
}

async function refreshSteward({poll = false} = {}) {
  if (!state.vaultId) return;
  stopWorkPoll();
  const generation = state.generation;
  const requestId = ++workState.refreshId;
  try {
    const result = await request(vaultPath('/steward'));
    if (generation !== state.generation || requestId !== workState.refreshId) return;
    const fingerprint = JSON.stringify(result);
    const changed = fingerprint !== workState.statusFingerprint;
    const wasUnconfigured = workState.unconfigured;
    workState.unconfigured = false;
    if (wasUnconfigured) renderWorkModels();
    workState.status = result;
    workState.statusFingerprint = fingerprint;
    if (!result.running && $('notice').dataset.kind === 'steward-running') showNotice('本轮已结束。请查看任务最终状态和加工知识；有异常的任务会单独列明。');
    if (changed) renderSteward(result);
    else updateWorkControls();
    if (changed && workState.project) await refreshKnowledge();
    if (generation !== state.generation || requestId !== workState.refreshId) return;
    if (result.running) workState.poll = setTimeout(() => refreshSteward({poll: true}).catch(() => {}), 2000);
  } catch (error) {
    if (generation !== state.generation || requestId !== workState.refreshId) return;
    workState.status = null;
    workState.statusFingerprint = null;
    workState.unconfigured = error.code === 'STEWARD_UNCONFIGURED';
    renderWorkModels();
    if (workState.unconfigured) {
      $('steward-status').textContent = '管家未配置：该服务尚未启用管家运行目录。当前记忆库已连接，不能加入队列或运行。';
      $('steward-status').className = 'run-status';
      $('steward-queue-count').textContent = '管家未配置';
      $('steward-source').textContent = '管家未配置；选择项目可核对已有资料与知识，不能加入队列。';
      $('steward-jobs').replaceChildren(node('div', '当前服务未启用管家队列；可继续查看原文和已有知识。', 'empty'));
      $('steward-round').replaceChildren();
      showNotice(errorMessage(error), true);
      return;
    }
    $('steward-status').textContent = '未取得后台状态；运行情况未知。恢复连接后手动刷新。';
    updateWorkControls();
    if (poll) showNotice(errorMessage(error), true);
    else throw error;
  }
}

function renderCitation(entry, knowledgeVersion) {
  const reference = node('div', undefined, 'knowledge-citation');
  const refs = Array.isArray(entry.sources) ? entry.sources : [entry];
  for (const source of refs) {
    const sourceVersion = source.sourceVersion ?? source.version;
    if (source.quote !== entry.text) reference.append(node('blockquote', source.quote || '未提供引用片段'));
    reference.append(node('p', `${source.path || '来源路径未提供'}${source.lineStart ? `:L${source.lineStart}–L${source.lineEnd ?? source.lineStart}` : ''}`, 'help mono'));
    reference.append(node('p', `来源 ${source.source?.id || source.recordId || '未提供'} · 原文 v${sourceVersion ?? '未知'} · 知识 v${knowledgeVersion ?? '未知'}`, 'help'));
    if (source.recordId && sourceVersion) {
      const button = node('button', '打开引用原文', 'text-button');
      button.type = 'button';
      button.addEventListener('click', () => loadRecord(source.recordId, sourceVersion, {switchPage: true}).catch((error) => showNotice(errorMessage(error), true)));
      reference.append(button);
    }
  }
  return reference;
}

function renderKnowledge() {
  const overview = workState.knowledge;
  const current = overview?.current;
  const viewing = workState.historical || current;
  const failed = workState.knowledgeLoad === 'failed';
  const loading = workState.knowledgeLoad === 'loading';
  $('knowledge-status').textContent = !workState.project ? '选择项目后读取真实知识版本。' : failed ? `读取知识失败；当前版本状态未知。${workState.knowledgeError} 草稿和事件标识已保留。` : loading ? '正在核对项目知识；已显示内容来自上次成功读取。' : !overview ? '尚未取得项目知识，请读取当前项目。' : !current ? '尚无加工知识。请在记忆管家加入队列并运行。' : `当前版本 v${current.version} · ${jobLabel(current.status)} · ${current.createdAt || '保存时间未提供'}`;
  $('knowledge-entry').textContent = overview?.entryPath ? `项目入口：${overview.entryPath}` : '';
  $('steward-source').textContent = workState.unconfigured ? '管家未配置；选择项目可核对已有资料与知识，不能加入队列。' : failed ? '当前项目资料状态读取失败，重新连接并核对后才可加入队列。' : overview ? `项目：${workState.project} · ${current ? `当前知识 v${current.version}（${jobLabel(current.status)}）` : '尚无加工知识'}${overview.sourceSignature ? ` · 资料签名 ${overview.sourceSignature.slice(0, 12)}` : ''}` : '请选择项目，读取资料状态后加入队列。';
  $('knowledge-version-title').textContent = viewing ? `${failed || loading ? '上次读取' : workState.historical ? '查看历史' : '当前知识'} · v${viewing.version}${failed || loading ? ' · 当前状态未核实' : ` · ${jobLabel(viewing.status)}`}` : failed ? '知识读取失败' : '当前知识';
  $('knowledge-show-current').hidden = !workState.historical;
  const entries = $('knowledge-entries');
  entries.replaceChildren();
  if (!viewing) entries.append(node('div', failed ? '本次未能读取版本，无法判断是否存在加工知识。请重新连接；已有草稿仍在下方。' : loading ? '正在请求项目知识。' : workState.project ? '没有可显示的知识版本。请先处理项目原文。' : '请选择项目。', 'empty'));
  else {
    if (viewing.status !== 'current') entries.append(node('p', `此版本为${jobLabel(viewing.status)}，不能当作当前已验证的解释。${viewing.reason ? `原因：${viewing.reason}` : ''}`, 'scope-note'));
    const items = Array.isArray(viewing.entries) ? viewing.entries : [];
    if (!items.length) entries.append(node('div', '该版本没有知识条目。请查看处理轨迹和版本原因。', 'empty'));
    for (const [index, entry] of items.entries()) {
      const card = node('article', undefined, 'knowledge-card');
      const heading = node('div', undefined, 'job-heading');
      const title = `摘录 ${index + 1}`;
      heading.append(node('h3', title), node('span', ({'source-statement': '原文摘录', 'human-correction': '人工纠正', fact: '事实', inference: '推论', unknown: '待确认', decision: '决定', correction: '人工纠正'})[entry.classification] || entry.classification || '未分类', 'tag'));
      card.append(heading, node('p', entry.text || '未提供文本', 'knowledge-text'), renderCitation(entry, viewing.version));
      const info = node('details', undefined, 'event-details');
      info.append(node('summary', '处理信息'), node('p', `条目 ID：${entry.entryId || '未提供'}`, 'help mono'));
      card.append(info);
      if (!workState.historical && current?.status === 'current' && entry.entryId) {
        const correct = node('button', '纠正此条', 'button');
        correct.type = 'button';
        correct.disabled = workState.busy || failed || loading || connectionState.phase !== 'ready';
        correct.addEventListener('click', () => {
          workState.correction = {entryId: entry.entryId, title, version: current.version, eventId: freshWorkEvent('correction-ui')};
          updateCorrectionGuard();
          $('correction-panel').scrollIntoView({block: 'center'});
          $('correction-text').focus();
        });
        card.append(correct);
      }
      entries.append(card);
    }
  }
  $('knowledge-versions').replaceChildren();
  for (const version of [...(overview?.versions || [])].reverse()) {
    const button = node('button', undefined, 'record-row');
    button.type = 'button';
    button.disabled = workState.busy || failed || loading || connectionState.phase !== 'ready';
    button.classList.toggle('selected', version.version === viewing?.version);
    button.append(node('b', `v${version.version} · ${jobLabel(version.status)}`), node('small', version.createdAt || '时间未提供'), node('small', version.reason || '未提供版本原因'));
    button.addEventListener('click', () => loadKnowledgeVersion(version.version).catch((error) => showNotice(errorMessage(error), true)));
    $('knowledge-versions').append(button);
  }
  if (!overview?.versions?.length) $('knowledge-versions').append(node('p', failed || loading ? '版本列表尚未取得。' : '尚无留存版本。', 'help'));
  updateWorkControls();
}

async function refreshKnowledge() {
  const project = workState.project;
  if (!project) { renderKnowledge(); return; }
  if ($('search-project').value === project) invalidateContext('正在核对知识状态，请在刷新后重新准备上下文。');
  const generation = state.generation;
  const requestId = ++workState.knowledgeRequest;
  workState.knowledgeLoad = 'loading';
  workState.knowledgeError = '';
  renderKnowledge();
  try {
    const result = await request(vaultPath('/knowledge') + '?' + new URLSearchParams({project}));
    if (generation !== state.generation || requestId !== workState.knowledgeRequest || project !== workState.project) return;
    const previousVersion = currentKnowledgeVersion();
    const previousStatus = workState.knowledge?.current?.status;
    workState.knowledge = result;
    workState.knowledgeLoad = 'ready';
    workState.knowledgeError = '';
    if (previousVersion !== currentKnowledgeVersion() || previousStatus !== result.current?.status) clearKnowledgeSearch();
    renderKnowledge();
  } catch (error) {
    if (generation !== state.generation || requestId !== workState.knowledgeRequest) return;
    workState.knowledgeLoad = 'failed';
    workState.knowledgeError = errorMessage(error);
    renderKnowledge();
    throw error;
  }
}

async function loadKnowledgeVersion(version) {
  const project = workState.project;
  const generation = state.generation;
  const requestId = ++workState.knowledgeRequest;
  const result = await request(vaultPath(`/knowledge/versions/${encodeURIComponent(version)}`) + '?' + new URLSearchParams({project}));
  if (generation !== state.generation || requestId !== workState.knowledgeRequest || project !== workState.project) return;
  if (version === currentKnowledgeVersion()) {
    workState.historical = null;
    workState.restore = null;
  } else {
    const summary = workState.knowledge?.versions?.find((item) => item.version === version) || {};
    workState.historical = {...summary, ...result.record, entries: result.entries || result.record?.entries || [], status: summary.status || result.record?.status || 'historical'};
    workState.restore = {version, expectedCurrentVersion: currentKnowledgeVersion(), eventId: freshWorkEvent('restore-ui')};
  }
  renderKnowledge();
}

function updateCorrectionGuard() {
  const target = workState.correction;
  const current = workState.knowledge?.current;
  const stale = Boolean(target && (!current || target.version !== current.version || current.status !== 'current'));
  $('correction-target').textContent = target ? `纠正目标：${workState.project} · 知识 v${target.version} · ${target.title || '已选摘录'}` : '尚未选择需要纠正的条目。';
  $('correction-event').textContent = target ? `${target.eventId}\n条目 ID：${target.entryId}` : '选择条目后生成';
  $('correction-submit').disabled = connectionState.phase !== 'ready' || connectionState.reconnecting || workState.busy || !target || target?.done || ((stale || workState.knowledgeLoad !== 'ready') && !target?.uncertain);
  $('correction-submit').textContent = target?.done ? '本次纠正已保存' : target?.uncertain ? '确认上次纠正提交结果' : '保存纠正并使旧解释失效';
  $('correction-text').readOnly = Boolean(target?.uncertain || target?.done);
  $('correction-guard').textContent = target?.done ? '本次纠正已保存，无需重复提交。下一步返回记忆管家，将最新资料加入队列并运行。' : target?.uncertain ? '上次提交结果尚未确认。再次点击会按原事件和原内容核实，不创建重复纠正。' : !target && current?.status === 'pending-review' ? '当前知识处于待复核状态。请返回记忆管家，将最新资料加入队列并运行。' : stale ? '目标已过期或处于待复核状态。请核对新版本并重新选择条目；输入内容已保留。' : '保存纠正后，旧解释立即进入待复核；返回管家重新入队并运行。';
  $('correction-next').hidden = !target?.done && current?.status !== 'pending-review';
  $('correction-next').disabled = connectionState.phase !== 'ready' || workState.busy;
}

function updateRestoreGuard() {
  const target = workState.restore;
  $('knowledge-restore-form').hidden = !target;
  const stale = Boolean(target && (!workState.knowledge || target.expectedCurrentVersion !== currentKnowledgeVersion()));
  $('knowledge-restore-target').textContent = target ? `恢复来源 v${target.version} · 预期当前 v${target.expectedCurrentVersion}` : '';
  $('knowledge-restore').disabled = connectionState.phase !== 'ready' || connectionState.reconnecting || workState.busy || !target || ((stale || workState.knowledgeLoad !== 'ready') && !target?.uncertain);
  $('knowledge-restore').textContent = target?.uncertain ? '确认上次恢复提交结果' : '恢复为新版本（待复核）';
  $('knowledge-restore-reason').readOnly = Boolean(target?.uncertain);
  $('knowledge-restore-guard').textContent = target?.uncertain ? '上次恢复结果尚未确认。再次点击会使用同一事件和原始内容核实。' : stale ? '当前版本已变化。请重新查看历史版本后再决定，不能用旧页面覆盖新结果。' : '恢复会新增待复核版本；随后重新入队加工。后台还会核验来源是否过期。';
}

async function runWorkAction(action) {
  if (workState.busy || contextState.saving || evidenceState.saving) return;
  if (contextState.preview) invalidateContext('知识操作开始，请完成后重新准备上下文。');
  workState.busy = true;
  const generation = state.generation;
  updateWorkControls();
  $('vault-select').disabled = true;
  try { await action(); }
  catch (error) {
    if (generation === state.generation) {
      showNotice(errorMessage(error), true);
      if (connectionState.phase === 'ready') {
        try { await refreshKnowledge(); } catch { /* Keep the original mutation error visible. */ }
      }
    }
  } finally {
    workState.busy = false;
    $('vault-select').disabled = false;
    if (workState.status) renderSteward(workState.status);
    renderKnowledge();
    updateWorkControls();
  }
}

function clearKnowledgeSearch() {
  workState.searchRequest += 1;
  $('knowledge-search-count').textContent = '尚未检索 / 请按当前范围重新检索';
  $('knowledge-search-results').replaceChildren();
}

async function searchKnowledge(event) {
  event.preventDefault();
  const project = workState.project;
  if (!project) { showNotice('请先选择一个项目。', true); return; }
  const generation = state.generation;
  const requestId = ++workState.searchRequest;
  const params = new URLSearchParams({project, q: $('knowledge-query').value.trim()});
  if ($('knowledge-search-history').checked) params.set('history', '1');
  try {
    const result = await request(vaultPath('/knowledge/search') + '?' + params);
    if (generation !== state.generation || requestId !== workState.searchRequest) return;
    $('knowledge-search-count').textContent = `${result.total ?? result.matches?.length ?? 0} 条匹配 · ${project}`;
    $('knowledge-search-results').replaceChildren();
    if (!result.matches?.length) $('knowledge-search-results').append(node('div', '没有匹配的可用知识。请核对项目、关键词或版本状态。', 'empty'));
    for (const [index, match] of (result.matches || []).entries()) {
      const card = node('article', undefined, 'knowledge-card');
      card.append(node('h3', `摘录 ${index + 1} · 知识 v${match.version} · ${jobLabel(match.status)}`), node('p', match.text || '', 'knowledge-text'), renderCitation({...match, version: match.sourceVersion}, match.version));
      const info = node('details', undefined, 'event-details');
      info.append(node('summary', '处理信息'), node('p', `条目 ID：${match.entryId || '未提供'}`, 'help mono'));
      card.append(info);
      $('knowledge-search-results').append(card);
    }
  } catch (error) {
    if (generation !== state.generation || requestId !== workState.searchRequest) return;
    $('knowledge-search-count').textContent = '检索失败';
    $('knowledge-search-results').replaceChildren();
    showNotice(errorMessage(error), true);
  }
}

function renderProjectGrants() {
  const profile = selectedModel();
  $('model-project-authorizations').replaceChildren();
  for (const grant of grants(profile)) {
    $('model-project-authorizations').append(node('p', `${grant.vaultId} · ${grant.project} · ${scopeLabel(grant.dataScope || 'synthetic')} · ${grant.dataPolicy === 'local-only' ? '仅本地' : '允许第三方'} · r${grant.revision}${grant.revision === profile.revision && grant.allow !== false ? ' · 有效' : ' · 已失效'}`, 'help'));
  }
  if (!grants(profile).length) $('model-project-authorizations').append(node('p', '当前连接尚无项目范围授权。', 'help'));
  const unavailable = connectionState.phase !== 'ready' || connectionState.reconnecting || modelState.busy || modelState.dirty || !profile || !state.vaultId;
  $('model-project-authorize').disabled = unavailable || !$('model-project-confirm').checked || !$('model-project-scope').value;
  $('model-project-revoke').disabled = unavailable || !$('model-project-scope').value;
  $('model-project-confirm').disabled = unavailable;
}

async function saveProjectGrant(allow) {
  if (allow && !$('model-project-confirm').checked) return;
  await runModelAction(async () => {
    const profile = selectedModel();
    const project = $('model-project-scope').value;
    if (!project) throw new Error('请先选择当前记忆库中的项目。');
    await request(modelPath('project-authorization'), {method: 'POST', body: JSON.stringify({expectedRevision: profile.revision, vaultId: state.vaultId, project, dataPolicy: $('model-project-policy').value, dataScope: currentDataScope(), allow})});
    $('model-project-confirm').checked = false;
    await refreshModels();
    showNotice(allow ? '项目处理授权已保存，尚未调用模型。返回记忆管家后可加入队列并运行。' : '所选范围的项目授权已撤销。');
  });
}

$('steward-project').addEventListener('change', () => { cancelNavigation(); setWorkProject($('steward-project').value).catch((error) => showNotice(errorMessage(error), true)); });
$('knowledge-project').addEventListener('change', () => { cancelNavigation(); setWorkProject($('knowledge-project').value).catch((error) => showNotice(errorMessage(error), true)); });
for (const id of ['steward-model', 'steward-policy']) $(id).addEventListener('change', () => { newQueueEvent(); updateWorkControls(); });
$('steward-new-event').addEventListener('click', newQueueEvent);
$('steward-refresh').addEventListener('click', () => runWorkAction(async () => { await refreshVault(); await refreshSteward(); if (!workState.unconfigured) { await refreshModels(); showNotice('已读取实际资料、模型与管家状态。'); } }));
$('knowledge-refresh').addEventListener('click', () => runWorkAction(async () => { showNotice(''); await refreshKnowledge(); }));
$('steward-enqueue-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runWorkAction(async () => {
    const profile = workModel();
    if (!profile || !workState.project || !workState.knowledge) throw new Error('请先读取项目状态并选择模型。');
    const payload = workState.queuePayload || {eventId: workState.queueEvent, project: workState.project, modelId: profile.id, dataPolicy: $('steward-policy').value};
    if (!workState.queuePayload && workState.knowledge.sourceSignature) payload.expectedSourceSignature = workState.knowledge.sourceSignature;
    workState.queuePayload = payload;
    const result = await request(vaultPath('/steward/enqueue'), {method: 'POST', body: JSON.stringify(payload)});
    await refreshSteward();
    showNotice(result.job?.status === 'no-work' ? '当前知识已经覆盖现有资料，后台登记为“没有新内容”，无需调用模型。' : result.duplicate ? '该事件已入队，没有重复创建任务。需要新的加工任务时点击“开始新任务”。' : '资料已加入真实处理队列。点击“开始这一轮”后才会调用模型。');
  });
});
$('steward-run-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runWorkAction(async () => {
    const result = await request(vaultPath('/steward/run'), {method: 'POST', body: JSON.stringify({maxJobs: Number($('steward-max-jobs').value), maxSeconds: Number($('steward-max-seconds').value)})});
    await refreshSteward();
    if (workState.status?.running) showNotice('本轮正在后台处理。以队列最终状态为准。', false, 'steward-running');
    else showNotice('本轮已结束。请查看任务最终状态和加工知识；有异常的任务会单独列明。');
  });
});
$('steward-pause').addEventListener('click', () => runWorkAction(async () => {
  const paused = !workState.status?.paused;
  await request(vaultPath('/steward/pause'), {method: 'POST', body: JSON.stringify({paused})});
  await refreshSteward();
  showNotice(paused ? '暂停请求已返回；已在途的模型请求以后台结果为准。' : '已恢复处理许可。只有主动开始新一轮才会继续。');
}));
$('knowledge-show-current').addEventListener('click', () => { workState.historical = null; workState.restore = null; renderKnowledge(); });
$('correction-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runWorkAction(async () => {
    const target = workState.correction;
    if (!target) throw new Error('请先选择当前知识条目。');
    const payload = target.payload || {eventId: target.eventId, project: workState.project, expectedKnowledgeVersion: target.version, targetEntryId: target.entryId, text: $('correction-text').value.trim()};
    if (!payload.text || new TextEncoder().encode(payload.text).length > 8192) throw new Error('纠正正文须为 1–8192 字节，请缩短后再提交。');
    target.payload = payload;
    target.uncertain = true;
    let result;
    try { result = await request(vaultPath('/corrections'), {method: 'POST', body: JSON.stringify(payload)}); }
    catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500 && error.httpStatus !== 408) { target.uncertain = false; target.payload = null; }
      throw error;
    }
    target.uncertain = false;
    target.done = true;
    if (result.current?.project) workState.knowledge = result.current;
    workState.historical = null;
    newQueueEvent();
    clearKnowledgeSearch();
    await refreshKnowledge();
    showNotice(result.duplicate ? '已确认同一纠正记录。请查看当前版本状态，再重新入队加工。' : '纠正已保存，旧解释已失效并进入待复核。请返回管家，将当前资料加入新任务并运行。');
  });
});
$('knowledge-restore-form').addEventListener('submit', (event) => {
  event.preventDefault();
  runWorkAction(async () => {
    const target = workState.restore;
    if (!target) throw new Error('请先选择要恢复的历史版本。');
    const payload = target.payload || {eventId: target.eventId, version: target.version, expectedCurrentVersion: target.expectedCurrentVersion, project: workState.project, reason: $('knowledge-restore-reason').value.trim()};
    target.payload = payload;
    target.uncertain = true;
    let result;
    try { result = await request(vaultPath('/knowledge/restore'), {method: 'POST', body: JSON.stringify(payload)}); }
    catch (error) {
      if (error.httpStatus >= 400 && error.httpStatus < 500 && error.httpStatus !== 408) { target.uncertain = false; target.payload = null; }
      throw error;
    }
    if (result.current?.project) workState.knowledge = result.current;
    workState.historical = null;
    workState.restore = null;
    newQueueEvent();
    clearKnowledgeSearch();
    await refreshKnowledge();
    showNotice('恢复请求已留存为待复核版本。请重新加入队列加工，不能直接把历史解释当作当前结论。');
  });
});
$('knowledge-search-form').addEventListener('submit', searchKnowledge);
$('knowledge-search-history').addEventListener('change', clearKnowledgeSearch);
$('knowledge-query').addEventListener('input', clearKnowledgeSearch);
$('model-project-authorization-form').addEventListener('submit', (event) => { event.preventDefault(); saveProjectGrant(true); });
$('model-project-revoke').addEventListener('click', () => saveProjectGrant(false));
for (const id of ['model-project-confirm', 'model-project-scope', 'model-project-policy']) $(id).addEventListener('change', renderProjectGrants);

$('models-refresh').addEventListener('click', () => runModelAction(() => refreshModels()));
$('model-new').addEventListener('click', () => { selectModel(null); showNotice('填写新连接后保存。保存配置不会调用模型。'); });
$('model-profile-form').addEventListener('submit', saveModelProfile);
$('model-profile-form').addEventListener('input', () => { modelState.dirty = true; updateModelControls(); });
$('model-kind').addEventListener('change', () => {
  $('model-rates-known').checked = false;
  for (const id of ['model-max-cost', 'model-input-price', 'model-output-price']) $(id).value = $('model-kind').value === 'local' ? '0' : '';
  modelState.dirty = true;
  updateModelControls();
});
$('model-credential-form').addEventListener('submit', saveModelCredential);
$('model-authorize-confirm').addEventListener('change', updateModelControls);
$('model-authorize').addEventListener('click', authorizeModel);
$('model-check').addEventListener('click', checkModel);
$('model-primary').addEventListener('click', () => runModelAction(async () => {
  const profile = selectedModel();
  await request('/api/models/primary', {method: 'POST', body: JSON.stringify({id: profile.id, expectedRevision: profile.revision})});
  await refreshModels();
  showNotice('已设为管家主模型。Codex 等工作智能体自身的配置没有变化。');
}));

for (const button of document.querySelectorAll('[data-page]')) button.addEventListener('click', () => { cancelNavigation(); showPage(button.dataset.page); });
$('open-steward-example').addEventListener('click', () => openNavigation({vault: 'm3', page: 'knowledge', project: '管家闭环-合成验收'}).catch((error) => showNotice(errorMessage(error), true)));
$('reconnect-button').addEventListener('click', reconnectService);
$('vault-select').addEventListener('change', () => {
  if (connectionState.reconnecting || contextState.saving || evidenceState.saving || workState.busy || modelState.busy || hasNavigationDraft()) {
    $('vault-select').value = state.vaultId || '';
    showNotice('当前有未完成操作或未提交表单，内容已保留。请先完成或清空表单，再切换记忆库。', true);
    return;
  }
  cancelNavigation();
  selectVault().catch((error) => showNotice(errorMessage(error), true));
});
$('records-project').addEventListener('change', () => { cancelNavigation(); syncNavigationLocation(); refreshRecords({resetDetail: true}).catch((error) => showNotice(errorMessage(error), true)); });
$('refresh-button').addEventListener('click', async () => {
  $('refresh-button').disabled = true;
  try { await refreshVault(); showNotice('已从本地服务刷新。'); }
  catch (error) { showNotice(errorMessage(error), true); }
  finally { $('refresh-button').disabled = false; }
});
$('reindex-button').addEventListener('click', reindex);
$('context-form').addEventListener('submit', prepareContext);
$('context-save').addEventListener('click', saveContext);
$('context-list-refresh').addEventListener('click', refreshContexts);
$('search-project').addEventListener('change', () => { clearContextReceipt(); refreshContexts(); });
for (const id of ['context-query', 'context-budget', 'search-project']) $(id).addEventListener(id === 'context-query' ? 'input' : 'change', () => invalidateContext());
$('evidence-form').addEventListener('submit', prepareEvidence);
$('evidence-save').addEventListener('click', saveEvidence);
$('evidence-list-refresh').addEventListener('click', refreshEvidenceList);
for (const id of ['evidence-query', 'evidence-budget', 'search-project']) $(id).addEventListener(id === 'evidence-query' ? 'input' : 'change', () => { invalidateEvidence(); if (id === 'search-project') { clearEvidenceReceipt(); refreshEvidenceList(); } });
$('search-form').addEventListener('submit', search);
for (const id of ['search-project', 'search-history']) $(id).addEventListener('change', () => {
  cancelNavigation();
  syncNavigationLocation();
  state.searchRequest += 1;
  $('search-count').textContent = '检索条件已变化';
  $('search-results').replaceChildren(node('div', '请按当前项目与版本范围重新检索。', 'empty'));
});
$('import-form').addEventListener('submit', importRecord);
for (const eventName of ['input', 'change']) $('import-form').addEventListener(eventName, () => { navigationState.importDirty = true; });
$('new-event-button').addEventListener('click', newEvent);
for (const radio of document.querySelectorAll('input[name="content-mode"]')) radio.addEventListener('change', updateContentMode);
$('import-file').addEventListener('change', () => {
  const file = $('import-file').files[0];
  if (file) $('import-filename').value = file.name;
});
$('copy-entry-button').addEventListener('click', async () => {
  if (!state.status?.entryPath) { showNotice('尚未取得真实入口路径。', true); return; }
  try { await navigator.clipboard.writeText(state.status.entryPath); showNotice('已复制真实入口路径。'); }
  catch { showNotice('浏览器未允许剪贴板访问，请选中入口路径手动复制。', true); }
});

async function bootstrap() {
  try {
    const result = await request('/api/bootstrap');
    state.token = result.csrfToken;
    state.vaults = result.vaults || [];
    connectionState.phase = 'ready';
    $('example-entry').hidden = !state.vaults.some((vault) => vault.id === 'm3');
    $('vault-select').replaceChildren();
    if (!result.vaults?.length) {
      $('vault-select').append(node('option', '没有已配置的记忆库'));
      $('connection-status').textContent = '服务已响应 · 无记忆库';
      $('record-list').replaceChildren(node('div', '尚未配置记忆库。请按启动说明指定独立目录。', 'empty'));
      showNotice('本地服务未配置可访问的记忆库。', true);
      return;
    }
    const placeholder = node('option', '请选择记忆库');
    placeholder.value = '';
    $('vault-select').append(placeholder);
    for (const vault of result.vaults) {
      const option = node('option', vaultDisplayName(vault));
      option.value = vault.id;
      $('vault-select').append(option);
    }
    $('vault-select').disabled = false;
    navigationState.ready = true;
    try { await openNavigation(parseNavigationQuery(window.location.search), {initial: true}); }
    catch (error) {
      $('connection-status').textContent = state.status ? '● 本地服务已响应 · 定位未完成' : '● 本地服务已响应 · 请选择定位';
      if (!state.status) {
        $('vault-select').value = '';
        $('steward-status').textContent = '定位未完成，未打开其他记忆库或项目。';
        $('steward-jobs').replaceChildren(node('div', '请从库列表选择有效范围，或使用管家闭环示例入口。', 'empty'));
        $('record-list').replaceChildren(node('div', '尚未打开记忆库。', 'empty'));
        $('knowledge-status').textContent = '定位未完成，尚未读取项目知识。';
      }
      showNotice(errorMessage(error), true);
    }
  } catch (error) {
    $('connection-status').textContent = '本地服务连接失败';
    $('vault-select').replaceChildren(node('option', '连接失败，请检查服务'));
    $('record-list').replaceChildren(node('div', '未取得资料列表。恢复服务后刷新页面。', 'empty'));
    showNotice(errorMessage(error), true);
  } finally {
    updateWorkControls();
  }
}

window.addEventListener('popstate', () => {
  if (!navigationState.ready) return;
  try { openNavigation(parseNavigationQuery(window.location.search)).catch((error) => showNotice(errorMessage(error), true)); }
  catch (error) { showNotice(errorMessage(error), true); }
});

newEvent();
renderCodexPayload();
updateContentMode();
bootstrap();
