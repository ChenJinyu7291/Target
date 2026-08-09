'use strict';

const $ = (id) => document.getElementById(id);
let currentProjectId = null;
let currentSnapshot = null;
let currentSessionId = null;
let currentSessionRole = 'researcher';
let pollTimer = null;
let kernelId = null;
let capabilities = null;
let webToken = localStorage.getItem('target_agent_web_token') || '';
let tokenPromptPromise = null;

async function api(path, options) {
  const init = Object.assign({}, options);
  const headers = new Headers(init.headers || {});
  if (webToken) headers.set('Authorization', 'Bearer ' + webToken);
  init.headers = headers;
  const response = await fetch(path, init);
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (response.status === 401 && payload && payload.error === 'unauthorized') {
    const token = await requestWebToken();
    if (token) return api(path, options);
    const err = new Error(payload.error || 'unauthorized');
    err.unauthorized = true;
    throw err;
  }
  if (!response.ok) {
    const err = new Error(payload && payload.error ? payload.error : `HTTP ${response.status}`);
    if (payload && payload.review_notes) err.review_notes = payload.review_notes;
    if (payload && payload.detail) {
      err.detail = payload.detail;
      err.message = `${err.message}：${payload.detail}`;
    }
    throw err;
  }
  return payload;
}

function requestWebToken() {
  if (!tokenPromptPromise) {
    tokenPromptPromise = showWebTokenPrompt();
    tokenPromptPromise.finally(() => { tokenPromptPromise = null; });
  }
  return tokenPromptPromise;
}

function showWebTokenPrompt() {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'token-overlay';
    overlay.innerHTML = `
      <div class="token-dialog" role="dialog" aria-modal="true" aria-label="Web access token">
        <h3>Access token required</h3>
        <p class="muted">This service requires the token configured via TARGET_AGENT_WEB_TOKEN.</p>
        <input id="token-input" type="password" placeholder="Bearer token" autocomplete="off" />
        <div class="token-actions">
          <button id="token-cancel" class="ghost" type="button">Cancel</button>
          <button id="token-save" class="primary" type="button">Save and continue</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#token-input');
    const finish = (value) => { overlay.remove(); resolve(value); };
    overlay.querySelector('#token-save').addEventListener('click', () => {
      const value = input.value.trim();
      if (!value) { input.focus(); return; }
      webToken = value;
      localStorage.setItem('target_agent_web_token', value);
      finish(value);
    });
    overlay.querySelector('#token-cancel').addEventListener('click', () => finish(null));
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) finish(null);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') overlay.querySelector('#token-save').click();
      if (event.key === 'Escape') finish(null);
    });
    input.focus();
  });
}

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function roleLabel(role) {
  return role === 'viewer' ? '只读查看' : role === 'reviewer' ? '审阅者' : role === 'admin' ? '管理员' : '研究员';
}

function currentActor() {
  if (!currentSessionId) return null;
  if (currentSessionRole === 'viewer') return null;
  return currentSessionRole;
}

function toast(message, kind = 'success') {
  const node = $('toast');
  node.textContent = message;
  node.className = `toast ${kind} show`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove('show'), 3200);
}

function shortValue(value) {
  if (value === null || value === undefined) return '';
  let text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 120 ? text.slice(0, 117) + '…' : text;
}

function statusClass(status) {
  switch (status) {
    case 'completed': return 'go';
    case 'completed_with_gaps': return 'conditional';
    case 'needs_input': case 'waiting_review': return 'conditional';
    case 'failed': case 'blocked': case 'cancelled': return 'danger';
    case 'running': case 'planned': case 'paused': case 'pending': return 'blue';
    default: return '';
  }
}

function resultStatusClass(status) {
  switch (status) {
    case 'completed': return 'go';
    case 'completed_with_gaps': return 'conditional';
    case 'needs_input': return 'conditional';
    case 'failed': case 'blocked': case 'skipped': return 'danger';
    default: return '';
  }
}

function countAttempts(attempts) {
  const counts = new Map();
  for (const row of attempts || []) {
    counts.set(row.work_item_id, (counts.get(row.work_item_id) || 0) + 1);
  }
  return counts;
}

function lineageIds(snap, itemId) {
  const byId = new Map((snap.plan && snap.plan.items || []).map((item) => [item.item_id, item]));
  const seen = new Set();
  let current = byId.get(itemId);
  while (current && current.rerun_of_item_id && !seen.has(current.rerun_of_item_id)) {
    seen.add(current.rerun_of_item_id);
    current = byId.get(current.rerun_of_item_id);
  }
  return new Set([itemId, ...seen]);
}

// ---------- capability / project list ----------

async function refreshCapabilities() {
  try {
    const caps = await api('/api/capabilities');
    capabilities = caps;
    const backends = Object.entries(caps.analysis_backends || {})
      .filter(([, enabled]) => enabled).map(([name]) => name).join(', ') || '无';
    const kernelOn = !!(caps.kernels && caps.kernels.enabled);
    $('capability').textContent = `科学合同 ${caps.contract_version || '?'} · 项目合同 ${caps.research_contract_version || '?'} · 后端 ${backends} · 技能库 ${(caps.skills && caps.skills.count) || 0} · 内核 ${kernelOn ? '开' : '关'}`;
    refreshCreatePreview();
  } catch (error) {
    $('capability').textContent = '系统能力不可用';
  }
}


// ---------- persistent analysis kernel ----------

async function refreshKernels() {
  const badge = $('kernel-status');
  const startBtn = $('kernel-start');
  const stopBtn = $('kernel-stop');
  const runBtn = $('kernel-run');
  try {
    const page = await api('/api/kernels');
    const list = page.kernels || [];
    const active = list.find((k) => k.status === 'ready' || k.status === 'busy') || null;
    const lastFailed = list.length && !active ? list[list.length - 1] : null;
    kernelId = active ? active.kernel_id : null;
    if (kernelId) {
      badge.textContent = active.language + ' 内核 ' + active.status + (active.exec_count ? ' · 已执行 ' + active.exec_count + ' 次' : '');
      badge.className = 'status-badge ' + (active.status === 'busy' ? 'blue' : 'go');
      startBtn.disabled = true;
      stopBtn.disabled = false;
      runBtn.disabled = false;
    } else if (lastFailed && lastFailed.status === 'failed') {
      badge.textContent = '内核失败' + (lastFailed.error ? '：' + lastFailed.error : '');
      badge.className = 'status-badge danger';
      startBtn.disabled = false;
      stopBtn.disabled = true;
      runBtn.disabled = true;
    } else {
      badge.textContent = '无内核';
      badge.className = 'status-badge';
      startBtn.disabled = false;
      stopBtn.disabled = true;
      runBtn.disabled = true;
    }
  } catch (error) {
    badge.textContent = '内核不可用';
    badge.className = 'status-badge danger';
    startBtn.disabled = true;
    stopBtn.disabled = true;
    runBtn.disabled = true;
    toast(error.message, 'error');
  }
}

async function startKernel() {
  const badge = $('kernel-status');
  try {
    $('kernel-start').disabled = true;
    badge.textContent = '正在启动 Python 内核…';
    badge.className = 'status-badge blue';
    const info = await api('/api/kernels', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: 'python' }),
    });
    kernelId = info.kernel_id;
    badge.textContent = 'Python 内核 ' + info.status;
    badge.className = 'status-badge go';
    $('kernel-stop').disabled = false;
    $('kernel-run').disabled = false;
    toast('内核已启动');
  } catch (error) {
    badge.textContent = '启动失败';
    badge.className = 'status-badge danger';
    $('kernel-start').disabled = false;
    toast(error.message, 'error');
  }
}

async function runKernelCode() {
  if (!kernelId) { toast('请先启动内核', 'error'); return; }
  const code = $('kernel-code').value;
  if (!code.trim()) { toast('请输入代码', 'error'); return; }
  const timeout = Number($('kernel-timeout').value) || 30;
  const output = $('kernel-output');
  output.textContent = '运行中…';
  try {
    const result = await api('/api/kernels/' + kernelId + '/exec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, timeout }),
    });
    const parts = [];
    if (result.stdout) parts.push(result.stdout);
    if (result.stderr) parts.push('stderr:\n' + result.stderr);
    if (result.traceback) parts.push('traceback:\n' + result.traceback);
    if (result.ok) {
      parts.push('result: ' + (result.result === undefined || result.result === null ? 'null' : JSON.stringify(result.result)));
    } else {
      parts.push('error: ' + (result.error || result.message || '执行失败'));
    }
    parts.push('[' + result.duration_ms + ' ms]' + (result.output_truncated ? '（输出已截断）' : ''));
    output.textContent = parts.join('\n\n');
    if (result.ok) toast('执行完成'); else toast('执行失败', 'error');
    refreshKernels();
  } catch (error) {
    output.textContent = '请求失败：' + error.message;
    toast(error.message, 'error');
    refreshKernels();
  }
}

async function stopKernel() {
  if (!kernelId) return;
  const id = kernelId;
  kernelId = null;
  try {
    await api('/api/kernels/' + id, { method: 'DELETE' });
    $('kernel-status').textContent = '已停止';
    $('kernel-status').className = 'status-badge';
    $('kernel-start').disabled = false;
    $('kernel-stop').disabled = true;
    $('kernel-run').disabled = true;
    toast('内核已停止');
  } catch (error) {
    toast(error.message, 'error');
    refreshKernels();
  }
}

async function loadProjects() {
  try {
    const data = await api('/api/projects');
    const rows = data.projects || [];
    const host = $('project-list');
    if (!rows.length) {
      host.innerHTML = '<p class="muted empty">还没有项目，先在右侧新建一个。</p>';
      return;
    }
    host.innerHTML = rows.map((row) => `
      <button class="project-card" data-id="${esc(row.project_id)}">
        <b>${esc(row.title)}</b>
        <small>${esc(row.project_id)} · ${esc(row.status)}</small>
      </button>`).join('');
    host.querySelectorAll('button[data-id]').forEach((button) => {
      button.addEventListener('click', () => selectProject(button.dataset.id));
    });
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function selectProject(projectId) {
  currentProjectId = projectId;
  $('workspace').classList.remove('hidden');
  $('workspace').scrollIntoView({ behavior: 'smooth', block: 'start' });
  await pollProject();
  await loadSessions(projectId);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollProject, 1500);
}

// ---------- create project ----------

let draftQuestion = null; // { goal_question, task_question, original_question }
let workflowTemplates = {}; // template_id -> {source_sha256, domain, modules, ...}

function projectDefaultsFor(workflowId) {
  const template = workflowTemplates[workflowId] || {};
  const limits = (capabilities && capabilities.limits) || {};
  return {
    contract_version: (capabilities && capabilities.research_contract_version) || undefined,
    task_contract_version: (capabilities && capabilities.contract_version) || undefined,
    max_work_items: template.max_work_items || undefined,
    max_replans: undefined,
    max_forks: undefined,
    max_tool_calls: limits.max_tool_calls || undefined,
  };
}

function refreshCreatePreview() {
  const host = $('create-preview');
  if (!host) return;
  const workflowId = $('workflow').value || 'disease_to_target';
  const defaults = projectDefaultsFor(workflowId);
  const template = workflowTemplates[workflowId] || {};
  const modules = (template.modules || []).join('、') || '默认模板';
  const maxWorkItems = defaults.max_work_items || '默认';
  const maxToolCalls = defaults.max_tool_calls || '默认';
  const autonomy = $('autonomy').value;
  const modeNote = autonomy === 'checkpointed'
    ? '关键节点审批：计划与发布需要人工批准'
    : autonomy === 'supervised'
      ? '逐步审批：每个工作项都需要人工批准'
      : '全自动：创建后将自动执行';
  host.textContent = `模板模块：${modules} · 限额：最多 ${maxWorkItems} 个工作项 / ${maxToolCalls} 次工具调用 · ${modeNote}`;
}

function buildProjectSpec() {
  const value = (id) => $(id).value.trim();
  const workflowId = value('workflow') || 'disease_to_target';
  const template = workflowTemplates[workflowId] || {};
  const disease = value('disease');
  const subtype = value('subtype');
  const tissue = value('tissue');
  const cell = value('cell');
  const stage = value('stage');
  const organism = value('organism');
  const assay = value('assay');
  const phenotype = value('phenotype');
  const accessions = value('accessions').split(',').map((row) => row.trim()).filter(Boolean);
  const rawQuestion = $('research-question-input').value.trim();
  const question = draftQuestion
    ? draftQuestion.goal_question
    : (rawQuestion || (disease ? `Which mechanisms and drug targets are supported by public evidence for ${disease}?` : 'Research question'));
  const defaults = projectDefaultsFor(workflowId);
  const base = {
    contract_version: defaults.contract_version,
    project_id: `project-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
    title: workflowId === 'literature_review' ? `${disease || 'literature'} research review` : `${disease} target discovery`,
    domain: template.domain || (workflowId === 'literature_review' ? 'life_science' : 'disease_target_discovery'),
    goal: {
      question,
      success_criteria: ['Every released conclusion is traceable to a durable artifact.'],
      deliverables: ['A reviewed research report with explicit evidence gaps.'],
      constraints: ['Only public data and allowlisted tools may be used.'],
    },
    autonomy_mode: value('autonomy'),
    max_work_items: defaults.max_work_items,
    max_replans: defaults.max_replans,
    max_forks: defaults.max_forks,
    workflow_template: workflowId,
    workflow_template_sha256: template.source_sha256 || null,
  };
  if (workflowId === 'literature_review') {
    base.context = { literature_query: draftQuestion ? draftQuestion.original_question : question };
    return base;
  }
  base.context = {
    disease, subtype, tissue, cell_type: cell, stage, phenotype, organism, assay,
    preferred_dataset_accessions: accessions,
    literature_query: `${disease} mechanism and drug targets`,
    target_task_spec: {
      contract_version: defaults.task_contract_version,
      task_type: 'disease_to_target',
      question: draftQuestion ? draftQuestion.task_question : `Which mechanisms and drug targets are supported by public evidence for ${disease}?`,
      context: {
        disease,
        disease_subtype: subtype || null,
        organism,
        tissue: tissue || null,
        cell_type: cell || null,
        disease_stage: stage || null,
        desired_phenotype: phenotype || null,
        assay: assay || null,
      },
      constraints: {
        public_data_only: true,
        dataset_selection: { preferred_dataset_accessions: accessions },
      },
    },
  };
  return base;
}

async function loadWorkflows() {
  try {
    const payload = await api('/api/workflows');
    const select = $('workflow');
    select.innerHTML = '';
    for (const row of (payload.workflows || [])) {
      workflowTemplates[row.template_id] = row;
      const option = document.createElement('option');
      option.value = row.template_id;
      option.textContent = row.template_id === 'disease_to_target'
        ? '疾病靶点发现（推荐）' : row.description;
      select.appendChild(option);
    }
    const saved = workflowTemplates['disease_to_target'];
    if (saved) select.value = 'disease_to_target';
    refreshCreatePreview();
  } catch (error) {
    console.warn('workflow catalog unavailable:', error);
  }
}
async function draftFromQuestion() {
  const question = $('research-question-input').value.trim();
  if (!question) { toast('请先输入研究问题', 'error'); return; }
  const hints = {};
  const diseaseHint = $('disease').value.trim();
  if (diseaseHint) hints.disease = diseaseHint;
  try {
    const draft = await api('/api/questions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, hints }),
    });
    const fields = draft.extracted || {};
    const set = (id, value) => { if (value) $(id).value = value; };
    set('disease', fields.disease);
    set('subtype', fields.disease_subtype);
    set('tissue', fields.tissue);
    set('cell', fields.cell_type);
    set('stage', fields.disease_stage);
    set('organism', fields.organism);
    set('phenotype', fields.desired_phenotype);
    const spec = draft.spec || {};
    const goal = spec.goal || {};
    const task = (spec.context && spec.context.target_task_spec) || {};
    draftQuestion = {
      goal_question: goal.question || question,
      task_question: task.question || goal.question || question,
      original_question: draft.question || question,
    };
    const notes = draft.review_notes || [];
    const badge = draft.needs_review ? '[需人工确认]' : '[可直接创建]';
    $('draft-review-notes').textContent = badge + ' ' + notes.join('; ');
    toast('AI 已解析问题，请确认表单后创建项目');
  } catch (error) {
    if (error && error.message && error.review_notes) {
      $('draft-review-notes').textContent = '需要补充信息: ' + error.review_notes;
    }
    toast(error.message || '解析失败', 'error');
  }
}
async function createProject() {
  const workflowId = $('workflow').value || 'disease_to_target';
  if (workflowId !== 'literature_review' && !$('disease').value.trim()) {
    toast('请填写疾病名称', 'error');
    return;
  }
  if (workflowId === 'literature_review' && !$('research-question-input').value.trim() && !$('disease').value.trim()) {
    toast('请填写研究问题或疾病名称', 'error');
    return;
  }
  const defaults = projectDefaultsFor(workflowId);
  const template = workflowTemplates[workflowId] || {};
  const modules = (template.modules || []).join('、') || '默认模板';
  const autonomy = $('autonomy').value;
  const modeNote = autonomy === 'checkpointed'
    ? '关键节点审批：计划与发布需要人工批准'
    : autonomy === 'supervised'
      ? '逐步审批：每个工作项都需要人工批准'
      : '全自动：创建后将自动执行（非关键节点审批模式）';
  const summary = `模板模块：${modules}\n限额：最多 ${defaults.max_work_items || '默认'} 个工作项 / ${defaults.max_tool_calls || '默认'} 次工具调用\n${modeNote}\n\n确定创建并启动项目？`;
  if (!window.confirm(summary)) return;
  const button = $('create');
  button.disabled = true;
  button.textContent = '正在创建…';
  try {
    const created = await api('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildProjectSpec()),
    });
    toast('项目已创建并启动');
    await loadProjects();
    await selectProject(created.project_id);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = '创建并启动项目';
  }
}

// ---------- snapshot rendering ----------

async function pollProject() {
  if (!currentProjectId) return;
  try {
    const snap = await api(`/api/projects/${currentProjectId}`);
    currentSnapshot = snap;
    renderSnapshot(snap);
    renderForkForm(snap);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderSnapshot(snap) {
  const state = snap.state || {};
  const status = state.status || 'draft';
  const badge = $('run-status');
  badge.textContent = status.toUpperCase();
  badge.className = `status-badge ${statusClass(status)}`;
  $('run-title').textContent = snap.spec.title;
  let meta = `${snap.spec.project_id} · ${snap.spec.autonomy_mode} · 覆盖层 ${snap.plan_revisions.length}`;
  if (state.terminal_reason) meta += ` · ${state.terminal_reason}`;
  if (state.checkpoint_kind) meta += ` · 等待: ${state.checkpoint_kind}`;
  $('run-meta').textContent = meta;
  const exportLink = $('export-package');
  if (exportLink) {
    exportLink.href = '/api/projects/' + snap.spec.project_id + '/export';
    exportLink.classList.remove('hidden');
  }
  const shareLink = $('share-portal');
  if (shareLink) {
    shareLink.href = '/api/projects/' + snap.spec.project_id + '/share';
    shareLink.classList.remove('hidden');
  }
  renderNextActions(snap);
  renderContext(snap);
  renderPlan(snap);
  renderStrategyPatterns(snap);
  renderPaperEvidence(snap);
  renderResults(snap);
  renderBranches(snap);
  renderArtifacts(snap);
  renderEvents(snap);
  renderGraph(snap);
  renderMechanismGraph(snap);
  renderFiles(snap);
  renderSessionActions(snap);
}

function renderNextActions(snap) {
  const host = $('next-actions');
  host.innerHTML = '';
  const actions = snap.next_actions || [];
  if (!actions.length) {
    host.innerHTML = '<span class="muted">无待办审批</span>';
    renderControlButtons(snap, host);
    return;
  }
  for (const action of actions) {
    if (action.action === 'decide_repair' || action.action === 'decide_fork') {
      const approve = document.createElement('button');
      approve.className = 'primary';
      approve.textContent = action.action === 'decide_repair' ? '批准修复' : '批准回退';
      approve.title = action.reason || '';
      approve.addEventListener('click', () => runAction(snap, action, true));
      host.appendChild(approve);
      const reject = document.createElement('button');
      reject.className = 'ghost';
      reject.textContent = action.action === 'decide_repair' ? '拒绝修复' : '拒绝回退';
      reject.addEventListener('click', () => runAction(snap, action, false));
      host.appendChild(reject);
      continue;
    }
    const button = document.createElement('button');
    button.className = 'primary';
    button.textContent = action.action === 'accept_checkpoint' ? '批准检查点' : '继续执行';
    button.title = action.reason || '';
    button.addEventListener('click', () => runAction(snap, action, true));
    host.appendChild(button);
  }
  renderControlButtons(snap, host);
}

function controlButton(label, cls, handler) {
  const button = document.createElement('button');
  button.className = cls;
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

function renderControlButtons(snap, host) {
  const status = snap.state ? snap.state.status : 'draft';
  const projectId = snap.spec.project_id;
  const actor = currentActor();
  const add = (label, cls, url, okText) => {
    const button = controlButton(label, cls, async () => {
      if (!actor) {
        toast('请先选择或新建一个可审批会话（研究员/审阅者/管理员）', 'error');
        return;
      }
      try {
        await api(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ actor, rationale: okText + '（工作台操作）' }),
        });
        toast(okText);
      } catch (error) {
        toast(error.message, 'error');
      }
      setTimeout(pollProject, 300);
    });
    host.appendChild(button);
  };
  if (status === 'running') {
    add('暂停', 'ghost', `/api/projects/${projectId}/pause`, '已请求暂停');
    add('取消', 'danger', `/api/projects/${projectId}/cancel`, '已请求取消');
  } else if (status === 'paused') {
    add('继续执行', 'primary', `/api/projects/${projectId}/resume`, '已请求继续');
    add('取消', 'danger', `/api/projects/${projectId}/cancel`, '已请求取消');
  } else if (['draft', 'planned', 'needs_input', 'waiting_review'].includes(status)) {
    add('取消项目', 'danger', `/api/projects/${projectId}/cancel`, '项目已取消');
  }
}

async function runAction(snap, action, approve) {
  const projectId = snap.spec.project_id;
  const actor = currentActor();
  if (!actor) {
    toast('请先选择或新建一个可审批会话（研究员/审阅者/管理员）', 'error');
    return;
  }
  try {
    if (action.action === 'accept_checkpoint') {
      await api(`/api/projects/${projectId}/decisions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_id: action.target_id,
          actor,
          rationale: action.reason || 'Approved from the web workbench.',
        }),
      });
      toast('检查点已批准，正在继续执行');
    } else if (action.action === 'decide_repair') {
      await api(`/api/projects/${projectId}/repairs/${action.repair_request_id}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          approve,
          actor,
          rationale: approve ? 'Approve the bounded repair.' : 'Reject the bounded repair.',
          trigger_snapshot_digest: action.trigger_snapshot_digest,
        }),
      });
      toast(approve ? '修复已批准，正在继续' : '修复已拒绝');
    } else if (action.action === 'decide_fork') {
      await api(`/api/projects/${projectId}/forks/${action.branch_id}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          approve,
          actor,
          rationale: approve ? 'Approve the rollback branch.' : 'Reject the rollback branch.',
        }),
      });
      toast(approve ? '回退已批准，正在继续' : '回退已拒绝');
    } else if (action.action === 'run_project' || action.action === 'resume_project') {
      await api(`/api/projects/${projectId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actor, rationale: 'Resumed from the web workbench.' }),
      });
      toast('已排队继续执行');
    }
  } catch (error) {
    toast(error.message, 'error');
  }
  setTimeout(pollProject, 300);
}

function renderContext(snap) {
  const context = snap.spec.context || {};
  const keys = ['disease', 'subtype', 'tissue', 'cell_type', 'stage', 'organism', 'assay', 'phenotype'];
  const cells = keys
    .filter((key) => context[key])
    .map((key) => `<div><span>${esc(key)}</span><b>${esc(context[key])}</b></div>`)
    .join('');
  $('research-question').textContent = snap.spec.goal.question;
  $('context').innerHTML = cells || '<p class="muted">无上下文</p>';
  $('planner-backend').textContent = `${snap.plan ? snap.plan.planner_backend : '未规划'} · 预算 工作项 ${snap.spec.max_work_items} / 修复 ${snap.spec.max_replans} / 回退 ${snap.spec.max_forks}`;
}

function renderPlan(snap) {
  const activeIds = new Set(snap.active_work_item_ids || []);
  const resultById = new Map((snap.work_item_results || []).map((row) => [row.item_id, row]));
  const attemptsByItem = countAttempts(snap.work_attempts || []);
  const items = (snap.plan && snap.plan.items) || [];
  $('plan').innerHTML = items.map((item, index) => {
    const active = activeIds.has(item.item_id);
    const result = resultById.get(item.item_id);
    const attempts = attemptsByItem.get(item.item_id) || 0;
    const status = result ? result.status : (active ? 'pending' : 'superseded');
    const bind = item.rerun_of_item_id
      ? ` · 重跑 ${esc(item.rerun_of_item_id)}${item.fork_branch_id ? ' · fork' : ''}`
      : '';
    return `<article class="plan-step">
      <span>${String(index + 1).padStart(2, '0')}</span>
      <div>
        <b>${esc(item.title)}</b>
        <small>${esc(item.item_id)} · ${esc(item.module)}${bind}</small>
        <i>依赖: ${esc((item.dependencies || []).join(', ') || '无')}</i>
      </div>
      <span class="status-badge ${resultStatusClass(status)}">${esc(status)} · ${attempts} 次尝试</span>
    </article>`;
  }).join('');
}

function renderStrategyPatterns(snap) {
  const patterns = (snap.plan && snap.plan.evidence_strategy_patterns) || [];
  const count = $('strategy-pattern-count');
  const host = $('strategy-patterns');
  if (!count || !host) return;
  count.textContent = patterns.length
    ? `命中 ${patterns.length} 个模式 · 来自近5年 CNS 论文，仅作策略提示`
    : '未命中论文模式（确定性流程不受影响）';
  host.innerHTML = patterns.length ? patterns.map((p) => `
    <article class="plan-step">
      <div>
        <b>${esc(p.name)}</b>
        <small>${esc(p.pattern_id)} · ${esc(p.validation_level)} · 起点 ${esc(p.chosen_start)} · 得分 ${esc(p.score)}</small>
        <i>证据顺序: ${esc((p.ordered_lanes || []).join(' → ') || '无')}</i>
        <i>为什么这个顺序: ${esc(p.why_this_order || '')}</i>
        <small>停止/降级规则: ${esc((p.stop_rules || []).join('; ') || '无')}</small>
        <small class="muted">策略提示非证据 · ${esc((p.matched_reason || []).join('; '))}</small>
      </div>
    </article>`).join('') : '<p class="muted empty">暂无模式</p>';
}

function renderPaperEvidence(snap) {
  const rows = (snap.plan && snap.plan.paper_evidence) || [];
  const count = $('paper-evidence-count');
  const host = $('paper-evidence');
  if (!count || !host) return;
  count.textContent = rows.length
    ? `命中 ${rows.length} 个论文分块 · 仅作策略提示`
    : '未检索到论文分块（确定性流程不受影响）';
  host.innerHTML = rows.length ? rows.map((row) => `
    <article class="plan-step">
      <div>
        <b>${esc(row.title)}</b>
        <small>${esc(row.chunk_id)} · ${esc(row.journal)} · ${esc(row.year)} · 得分 ${esc(row.score)}</small>
        <i>${esc(row.snippet || '')}</i>
        <small>证据层: ${esc((row.lane_tags || []).join(' · ') || '无')} · 命中原因: ${esc((row.matched_reason || []).join('; '))}</small>
        <small class="muted">策略提示非证据 · PMID ${esc(row.pmid)}</small>
      </div>
    </article>`).join('') : '<p class="muted empty">暂无论文证据</p>';
}

function renderResults(snap) {
  const activeIds = new Set(snap.active_work_item_ids || []);
  const rows = (snap.work_item_results || []).filter((row) => activeIds.has(row.item_id));
  $('results').innerHTML = rows.length ? rows.map((row) => {
    const outputs = Object.entries(row.outputs || {})
      .map(([key, value]) => `${esc(key)}=${esc(shortValue(value))}`).join(' · ');
    const limitations = (row.limitations || []).map(esc).join('; ');
    return `<article class="result-row">
      <div class="result-head"><b>${esc(row.item_id)}</b><span class="status-badge ${resultStatusClass(row.status)}">${esc(row.status)}</span></div>
      <p>${esc(row.summary)}</p>
      ${outputs ? `<small class="mono">${outputs}</small>` : ''}
      ${row.error ? `<small class="danger-text">错误: ${esc(row.error)}</small>` : ''}
      ${limitations ? `<small class="muted">${limitations}</small>` : ''}
      ${row.fork_branch_id ? `<small class="muted">分支 ${esc(row.fork_branch_id)} · 覆盖 ${esc(row.supersedes_result_digest ? row.supersedes_result_digest.slice(0, 12) : '')}…</small>` : ''}
    </article>`;
  }).join('') : '<p class="muted empty">暂无结果</p>';
}

function renderBranches(snap) {
  const host = $('branches');
  const branches = snap.plan_branches || [];
  const directives = new Map((snap.fork_directives || []).map((row) => [row.fork_directive_id, row]));
  if (!branches.length) {
    host.innerHTML = '<p class="muted empty">尚无回退分支</p>';
    return;
  }
  host.innerHTML = `<div class="branch-table">${branches.map((branch) => {
    const directive = directives.get(branch.fork_directive_id);
    const pending = branch.status === 'proposed';
    const attempts = branch.rollback_to_attempt_id ? ` · 恢复 ${esc(branch.rollback_to_attempt_id)}` : '';
    return `<div class="branch-row">
      <div><b>${esc(branch.branch_id)}</b><small>${esc(branch.mode)} · 目标 ${esc(branch.fork_point_item_id)}${attempts} · ${esc(branch.status)}</small></div>
      <small class="muted">${esc(directive ? directive.rationale : '')}</small>
      <div class="branch-actions">${pending
        ? `<button data-branch="${esc(branch.branch_id)}" data-approve="true" class="primary">批准</button>
           <button data-branch="${esc(branch.branch_id)}" data-approve="false" class="ghost">拒绝</button>`
        : ''}</div>
    </div>`;
  }).join('')}</div>`;
  host.querySelectorAll('button[data-branch]').forEach((button) => {
    button.addEventListener('click', () => {
      decideFork(snap.spec.project_id, button.dataset.branch, button.dataset.approve === 'true');
    });
  });
}

async function decideFork(projectId, branchId, approve) {
  const actor = currentActor();
  if (!actor) {
    toast('请先选择或新建一个可审批会话（研究员/审阅者/管理员）', 'error');
    return;
  }
  try {
    await api(`/api/projects/${projectId}/forks/${branchId}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        approve,
        actor,
        rationale: approve ? 'Approve the rollback branch.' : 'Reject the rollback branch.',
      }),
    });
    toast(approve ? '回退已批准，正在继续' : '回退已拒绝');
  } catch (error) {
    toast(error.message, 'error');
  }
  setTimeout(pollProject, 300);
}

function renderArtifacts(snap) {
  const rows = snap.artifacts || [];
  $('artifacts').innerHTML = rows.length ? rows.map((row) => `
    <div class="artifact-row">
      <div><b>${esc(row.logical_name)}</b><small>${esc(row.media_type)} · ${row.size_bytes} B · sha256 ${esc(row.sha256.slice(0, 12))}…</small></div>
      <a class="ghost link-button" href="/api/projects/${esc(snap.spec.project_id)}/artifacts/${esc(row.artifact_id)}">下载</a>
    </div>`).join('') : '<p class="muted empty">暂无产物</p>';
}

async function renderEvents(snap) {
  try {
    const page = await api(`/api/projects/${snap.spec.project_id}/events?after_sequence=0`);
    const rows = page.events || [];
    $('event-count').textContent = `${rows.length} 条`;
    const tail = rows.slice(-80).reverse();
    $('events').innerHTML = tail.map((row) => `
      <div class="event-row">
        <span>${row.sequence}</span>
        <div>
          <b>${esc(row.event_type)} · ${esc(row.state)}</b>
          ${row.work_item_id ? `<small>${esc(row.work_item_id)}</small>` : ''}
          <small>${esc(JSON.stringify(row.detail || {}))}</small>
        </div>
      </div>`).join('');
  } catch (error) {
    $('events').innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

// ---------- fork form ----------

function renderForkForm(snap) {
  const activeIds = new Set(snap.active_work_item_ids || []);
  const resultById = new Map((snap.work_item_results || []).map((row) => [row.item_id, row]));
  const targets = [...resultById.keys()].filter((itemId) => {
    const row = resultById.get(itemId);
    return activeIds.has(itemId) && row.status in { completed: 1, completed_with_gaps: 1 };
  });
  $('fork-target').innerHTML = targets.length
    ? targets.map((itemId) => `<option value="${esc(itemId)}">${esc(itemId)}</option>`).join('')
    : '<option value="">暂无可回退工作项</option>';
  renderForkAttempts(snap);
}

function renderForkAttempts(snap) {
  const target = $('fork-target').value;
  const host = $('fork-attempt');
  if ($('fork-mode').value !== 'restore' || !target) {
    host.innerHTML = '<option value="">—</option>';
    return;
  }
  const allowed = lineageIds(snap, target);
  const rows = (snap.work_attempts || []).filter((row) =>
    (row.status === 'completed' || row.status === 'completed_with_gaps') && allowed.has(row.work_item_id)
  );
  host.innerHTML = rows.length
    ? rows.map((row) => `<option value="${esc(row.attempt_id)}">${esc(row.work_item_id)} #${row.attempt_number} · ${esc(row.attempt_id)}</option>`).join('')
    : '<option value="">无历史 attempt</option>';
}

async function proposeFork() {
  const target = $('fork-target').value;
  const mode = $('fork-mode').value;
  const rationale = $('fork-rationale').value.trim();
  if (!target || !rationale) {
    toast('请选择目标工作项并填写理由', 'error');
    return;
  }
  let inputOverrides = null;
  const raw = $('fork-overrides').value.trim();
  if (raw) {
    try { inputOverrides = JSON.parse(raw); }
    catch (_) { toast('输入覆盖必须是合法 JSON', 'error'); return; }
  }
  const actor = currentActor();
  if (!actor) {
    toast('只读会话不能发起回退', 'error');
    return;
  }
  const body = {
    target_work_item_id: target,
    mode,
    rationale,
    actor,
  };
  if (mode === 'restore') body.rollback_to_attempt_id = $('fork-attempt').value || null;
  if (inputOverrides) body.input_overrides = inputOverrides;
  try {
    await api(`/api/projects/${currentProjectId}/forks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast('回退分支已发起');
    $('fork-rationale').value = '';
    $('fork-overrides').value = '';
  } catch (error) {
    toast(error.message, 'error');
  }
  setTimeout(pollProject, 300);
}

// ---------- research session ----------

async function loadSessions(projectId) {
  const data = await api(`/api/projects/${projectId}/sessions`);
  let sessions = data.sessions || [];
  const host = $('session-list');
  if (!sessions.length) {
    const created = await api(`/api/projects/${projectId}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '研究对话', role: $('new-session-role') ? $('new-session-role').value : currentSessionRole }),
    });
    sessions = [created.session];
    toast('已自动创建研究会话');
  }
  host.innerHTML = sessions.map((row) => `
    <button class="session-card${row.session_id === currentSessionId ? ' active' : ''}" data-session="${esc(row.session_id)}" data-role="${esc(row.role || 'researcher')}">
      <b>${esc(row.title)}</b>
      <small>${roleLabel(row.role)} · ${row.message_count} 条消息</small>
    </button>`).join('');
  host.querySelectorAll('button[data-session]').forEach((button) => {
    button.addEventListener('click', () => selectSession(projectId, button.dataset.session));
  });
  if (!sessions.some((row) => row.session_id === currentSessionId)) {
    currentSessionId = sessions[0].session_id;
  }
  const activeSession = sessions.find((row) => row.session_id === currentSessionId) || {};
  currentSessionRole = activeSession.role || 'researcher';
  await loadSessionMessages(projectId, currentSessionId);
}

async function selectSession(projectId, sessionId) {
  currentSessionId = sessionId;
  await loadSessions(projectId);
}

async function loadSessionMessages(projectId, sessionId) {
  const host = $('session-messages');
  if (!sessionId) {
    host.innerHTML = '<p class="muted">等待选择会话…</p>';
    return;
  }
  const data = await api(`/api/projects/${projectId}/sessions/${sessionId}`);
  const messages = data.messages || [];
  if (!messages.length) {
    host.innerHTML = '<p class="muted">会话为空，开始提问吧。</p>';
    return;
  }
  host.innerHTML = messages.map((row) => {
    const cls = row.role === 'user' ? 'user' : row.role === 'system' ? 'system' : 'assistant';
    const tag = row.kind === 'intervention_result' ? '决策结果'
      : row.kind === 'intervention' ? '审批指令'
      : row.kind === 'answer' ? 'Agent 摘要' : '';
    return `<div class="msg ${cls}"><div class="msg-head">${esc(row.role)}${tag ? `<small>${esc(tag)}</small>` : ''}</div><div class="msg-text">${esc(row.text).replaceAll('\n', '<br>')}</div></div>`;
  }).join('');
  host.scrollTop = host.scrollHeight;
}

async function sendSessionMessage(askAgent) {
  if (!currentProjectId || !currentSessionId) {
    toast('请先选择项目与会话', 'error');
    return;
  }
  const input = $('session-input');
  const text = input.value.trim();
  if (!text) {
    toast('消息不能为空', 'error');
    return;
  }
  try {
    await api(`/api/projects/${currentProjectId}/sessions/${currentSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, ask_agent: askAgent, actor: currentSessionRole || 'researcher' }),
    });
    input.value = '';
    await loadSessionMessages(currentProjectId, currentSessionId);
    if (askAgent) toast('Agent 摘要已生成');
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderSessionActions(snap) {
  const host = $('session-actions');
  host.innerHTML = '';
  if (currentSessionRole === 'viewer') {
    const hint = document.createElement('span');
    hint.className = 'muted';
    hint.textContent = '只读会话：可提问与查看，不能审批、修复或补充输入。';
    host.appendChild(hint);
    const box = $('session-supplement');
    if (box) box.classList.add('hidden');
    return;
  }
  const state = snap.state || {};
  if (state.status === 'needs_input' && state.current_item_id) {
    const supplementBtn = document.createElement('button');
    supplementBtn.className = 'ghost';
    supplementBtn.type = 'button';
    supplementBtn.textContent = '补充输入并重跑';
    supplementBtn.title = `目标工作项：${state.current_item_id}`;
    supplementBtn.addEventListener('click', () => {
      const box = $('session-supplement');
      box.classList.toggle('hidden');
      if (!box.classList.contains('hidden')) {
        box.dataset.targetItem = state.current_item_id;
        $('session-supplement-json').focus();
      }
    });
    host.appendChild(supplementBtn);
  }
  const actions = snap.next_actions || [];
  if (!actions.length) {
    const hint = document.createElement('span');
    hint.className = 'muted';
    hint.textContent = state.status === 'needs_input' ? '等待补充输入后重跑' : '当前没有待办审批';
    host.appendChild(hint);
    return;
  }
  const intro = document.createElement('span');
  intro.className = 'muted';
  intro.textContent = '会话内审批：';
  host.appendChild(intro);
  for (const action of actions) {
    if (action.action === 'accept_checkpoint') {
      host.appendChild(sessionActionButton(snap, action, true, '批准检查点'));
    } else if (action.action === 'decide_repair') {
      host.appendChild(sessionActionButton(snap, action, true, '批准修复'));
      host.appendChild(sessionActionButton(snap, action, false, '拒绝修复'));
    } else if (action.action === 'decide_fork') {
      host.appendChild(sessionActionButton(snap, action, true, '批准回退'));
      host.appendChild(sessionActionButton(snap, action, false, '拒绝回退'));
    }
  }
}

function sessionActionButton(snap, action, approve, label) {
  const button = document.createElement('button');
  button.className = approve ? 'primary' : 'ghost';
  button.type = 'button';
  button.textContent = label;
  button.title = action.reason || '';
  button.addEventListener('click', () => runSessionIntervention(snap, action, approve));
  return button;
}

async function runSessionIntervention(snap, action, approve) {
  if (!currentProjectId) return;
  const rationale = $('session-input').value.trim()
    || (approve ? '在会话中批准该决策。' : '在会话中拒绝该决策。');
  let sessionId = currentSessionId;
  try {
    if (!sessionId) {
      const created = await api(`/api/projects/${currentProjectId}/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '研究对话', role: $('new-session-role') ? $('new-session-role').value : currentSessionRole }),
      });
      sessionId = created.session.session_id;
      currentSessionId = sessionId;
      currentSessionRole = created.session.role || 'researcher';
    }
    const targetId = action.action === 'decide_repair' ? action.repair_request_id
      : action.action === 'decide_fork' ? action.branch_id : action.target_id;
    const body = {
      action: action.action,
      rationale,
      actor: currentSessionRole,
      target_id: targetId,
    };
    if (action.action === 'decide_repair') {
      body.approve = approve;
      body.snapshot_digest = action.trigger_snapshot_digest;
    }
    if (action.action === 'decide_fork') body.approve = approve;
    await api(`/api/projects/${currentProjectId}/sessions/${sessionId}/interventions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    $('session-input').value = '';
    toast(approve ? '决策已批准并记录到会话' : '决策已拒绝并记录到会话');
  } catch (error) {
    toast(error.message, 'error');
  }
  await loadSessions(currentProjectId);
  setTimeout(pollProject, 300);
}

async function runSessionSupplement() {
  if (!currentProjectId || !currentSessionId) {
    toast('请先选择项目与会话', 'error');
    return;
  }
  const box = $('session-supplement');
  const targetItem = box.dataset.targetItem;
  if (!targetItem) {
    toast('请先点击“补充输入并重跑”', 'error');
    return;
  }
  let overrides = null;
  const raw = $('session-supplement-json').value.trim();
  if (raw) {
    try { overrides = JSON.parse(raw); } catch (_) {
      toast('补充输入必须是合法 JSON', 'error');
      return;
    }
  }
  const rationale = $('session-input').value.trim() || '在会话中补充输入并重跑该工作项。';
  try {
    await api(`/api/projects/${currentProjectId}/sessions/${currentSessionId}/interventions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'propose_fork',
        rationale,
        actor: currentSessionRole,
        target_id: targetItem,
        mode: 'redo',
        input_overrides: overrides,
      }),
    });
    box.classList.add('hidden');
    box.dataset.targetItem = '';
    $('session-supplement-json').value = '';
    $('session-input').value = '';
    toast('已发起补充输入回退，等待批准');
  } catch (error) {
    toast(error.message, 'error');
  }
  await loadSessions(currentProjectId);
  setTimeout(pollProject, 300);
}// ---------- init ----------

async function init() {
  refreshCapabilities();
  await loadProjects();
  $('refresh-projects').addEventListener('click', loadProjects);
  $('create').addEventListener('click', createProject);
  loadWorkflows();
  $('draft-from-question').addEventListener('click', draftFromQuestion);
  $('propose-fork').addEventListener('click', proposeFork);
  $('new-session').addEventListener('click', async () => {
    if (!currentProjectId) { toast('请先选择项目', 'error'); return; }
    const created = await api(`/api/projects/${currentProjectId}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '研究对话', role: $('new-session-role') ? $('new-session-role').value : currentSessionRole }),
    });
    currentSessionId = created.session.session_id;
    currentSessionRole = created.session.role || 'researcher';
    await loadSessions(currentProjectId);
  });
  $('session-send').addEventListener('click', () => sendSessionMessage(false));
  $('session-ask').addEventListener('click', () => sendSessionMessage(true));
  $('session-supplement-run').addEventListener('click', runSessionSupplement);
  $('fork-mode').addEventListener('change', () => {
    if (currentSnapshot) renderForkAttempts(currentSnapshot);
  });
  $('workflow').addEventListener('change', refreshCreatePreview);
  $('autonomy').addEventListener('change', refreshCreatePreview);
  $('kernel-start').addEventListener('click', startKernel);
  $('kernel-stop').addEventListener('click', stopKernel);
  $('kernel-run').addEventListener('click', runKernelCode);
  $('tab-dag').addEventListener('click', () => switchGraphTab('dag'));
  $('tab-mechanism').addEventListener('click', () => switchGraphTab('mechanism'));
  refreshKernels();
}

document.addEventListener('DOMContentLoaded', init);

function switchGraphTab(tab) {
  const isDag = tab === 'dag';
  $('tab-dag').classList.toggle('active', isDag);
  $('tab-mechanism').classList.toggle('active', !isDag);
  $('dag-graph').classList.toggle('hidden', !isDag);
  $('mechanism-graph').classList.toggle('hidden', isDag);
}


// ---------- evidence graph / file preview ----------

async function renderGraph(snap) {
  const host = $('dag-graph');
  const projectId = snap.spec.project_id;
  try {
    const graph = await api(`/api/projects/${projectId}/graph`);
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    if (!nodes.length) {
      host.innerHTML = '<p class="muted">尚无计划节点（项目未规划）</p>';
      return;
    }
    host.innerHTML = renderSvgGraph(nodes, edges);
  } catch (error) {
    host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

function renderSvgGraph(nodes, edges) {
  const work = nodes.filter((n) => n.kind === 'work_item');
  const artifactNodes = nodes.filter((n) => n.kind === 'artifact');
  const branchNodes = nodes.filter((n) => n.kind === 'branch');
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const deps = edges.filter((e) => e.kind === 'depends_on');
  const level = new Map();
  const compute = (id) => {
    if (level.has(id)) return level.get(id);
    const node = byId.get(id);
    let lv = 0;
    if (node && node.kind === 'work_item') {
      for (const e of deps) if (e.target === id) lv = Math.max(lv, compute(e.source) + 1);
    }
    level.set(id, lv);
    return lv;
  };
  work.forEach((n) => compute(n.id));
  const maxWorkLevel = Math.max(0, ...work.map((n) => level.get(n.id)));
  for (const n of artifactNodes) {
    const producer = edges.find((e) => e.target === n.id && e.kind === 'produces');
    level.set(n.id, producer ? (level.get(producer.source) || 0) + 1 : maxWorkLevel + 1);
  }
  const colW = 210, rowH = 64, nodeW = 180, nodeH = 42, padX = 24, padY = 20;
  const maxLevel = Math.max(0, ...nodes.map((n) => level.get(n.id) || 0));
  const cols = [];
  nodes.forEach((n) => {
    const lv = level.get(n.id) || maxLevel;
    (cols[lv] = cols[lv] || []).push(n);
  });
  const maxRows = Math.max(1, ...cols.map((c) => c.length));
  const width = (maxLevel + 1) * colW + padX * 2;
  const height = maxRows * rowH + padY * 2 + 34;
  const pos = new Map();
  cols.forEach((col, lv) => {
    col.forEach((n, idx) => {
      const cy = ((maxRows - col.length) / 2) * rowH + idx * rowH + padY;
      pos.set(n.id, { x: padX + lv * colW, y: cy });
    });
  });
  const statusColor = (s) => ({
    completed: '#42d392', completed_with_gaps: '#ffbf69', running: '#5da9ff',
    pending: '#98abc3', failed: '#ff6b7a', blocked: '#ff6b7a',
  }[s] || '#98abc3');
  const edgeSvg = edges.map((e) => {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return '';
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2;
    const x2 = b.x, y2 = b.y + nodeH / 2;
    const color = e.kind === 'produces' ? '#8ec5ff' : '#5f7ea0';
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="1.4" marker-end="url(#arrow)"/>`;
  }).join('');
  const nodeSvg = nodes.map((n) => {
    const p = pos.get(n.id);
    if (!p) return '';
    const isWork = n.kind === 'work_item';
    const fill = isWork ? statusColor(n.status) : '#2b6f9e';
    const stroke = n.active ? '#eef6ff' : 'none';
    const title = isWork ? (n.module + ' · ' + n.status) : (n.media_type + ' · ' + n.sha256_short);
    const label = n.label.length > 26 ? n.label.slice(0, 25) + '…' : n.label;
    return `<g transform="translate(${p.x},${p.y})">
      <rect width="${nodeW}" height="${nodeH}" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="1.5" opacity="0.92"/>
      <text x="${nodeW / 2}" y="18" text-anchor="middle" fill="#06121f" font-size="11" font-weight="700">${esc(label)}</text>
      <text x="${nodeW / 2}" y="33" text-anchor="middle" fill="#0b1b2e" font-size="9">${esc(title)}</text>
    </g>`;
  }).join('');
  const legend = `<div class="graph-legend">工作项：<span class="dot" style="background:#42d392"></span>完成
    <span class="dot" style="background:#ffbf69"></span>完成有缺口 <span class="dot" style="background:#5da9ff"></span>运行
    <span class="dot" style="background:#98abc3"></span>待处理 <span class="dot" style="background:#ff6b7a"></span>失败/阻断
    · 产物：<span class="dot" style="background:#2b6f9e"></span>artifact · 白框=活跃</div>`;
  const branchNote = branchNodes.length
    ? `<p class="muted">回退分支：${branchNodes.map((b) => esc(b.label) + ' (' + esc(b.mode) + '/' + esc(b.status) + ')').join('，')}</p>`
    : '';
  return `${legend}${branchNote}<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" style="max-width:100%">
    <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#5f7ea0"/></marker></defs>
    ${edgeSvg}${nodeSvg}</svg>`;
}


// ---------- mechanism evidence graph ----------

let mechanismPayload = null;
let mechanismFilters = { patternLinks: true, predictedInferred: true, lanes: true };

async function renderMechanismGraph(snap) {
  const host = $('mechanism-graph');
  const projectId = snap.spec.project_id;
  try {
    const payload = await api(`/api/projects/${projectId}/mechanism-graph`);
    mechanismPayload = payload;
    host.innerHTML = renderMechanismPanel(payload);
    host.querySelectorAll('input[data-filter]').forEach((input) => {
      input.addEventListener('change', () => {
        mechanismFilters[input.dataset.filter] = input.checked;
        if (mechanismPayload) host.innerHTML = renderMechanismPanel(mechanismPayload);
      });
    });
  } catch (error) {
    host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

function renderMechanismPanel(payload) {
  if (!payload.available) {
    return `<p class="muted">${esc(payload.reason || '机制证据图暂不可用')}</p>`;
  }
  const graph = payload.graph;
  const nodes = (graph && graph.nodes) || [];
  const edges = (graph && graph.edges) || [];
  const stats = (graph && graph.model_statistics) || {};
  if (!nodes.length) {
    return `<p class="muted">尚无机制证据节点（项目未完成靶点发现，或证据为空）。</p>`;
  }
  const filtered = filterMechanismEdges(nodes, edges);
  const svg = mechanismSvg(nodes, filtered.edges);
  const findings = (payload.synthesis_findings || []).slice(0, 8);
  const reviewer = (payload.reviewer_findings || []).filter((row) => row.severity === 'blocking' || row.severity === 'major').slice(0, 8);
  const links = (payload.pattern_links || []).slice(0, 8);
  const chips = mechanismLaneChips(payload.lane_coverage, payload.ranked_genes || []);
  const controls = `<div class="graph-tools">
    <label><input type="checkbox" data-filter="patternLinks" ${mechanismFilters.patternLinks ? 'checked' : ''}> 模式跨层链接</label>
    <label><input type="checkbox" data-filter="predictedInferred" ${mechanismFilters.predictedInferred ? 'checked' : ''}> 预测/推断边</label>
    <label><input type="checkbox" data-filter="lanes" ${mechanismFilters.lanes ? 'checked' : ''}> 证据层节点</label>
    <span class="muted">边权重是上下文匹配系数，用于排序，不代表临床成功概率。论文RAG命中仅作策略提示（strategy_only），不是证据，不进入排序。</span>
  </div>`;
  const statsRow = `<div class="metric-row">
    <div class="metric cyan"><strong>${esc(stats.evidence_items ?? 0)}</strong><span>证据条目</span></div>
    <div class="metric blue"><strong>${esc(stats.ranked_genes ?? 0)}</strong><span>候选基因</span></div>
    <div class="metric green"><strong>${esc(stats.pattern_links ?? 0)}</strong><span>模式跨层链接</span></div>
    <div class="metric amber"><strong>${esc(stats.conflicting_genes ? stats.conflicting_genes.length : 0)}</strong><span>方向冲突基因</span></div>
    <div class="metric purple"><strong>${esc(stats.dependent_links_withheld ?? 0)}</strong><span>依赖链接拦截</span></div>
    <div class="metric"><strong>${esc(stats.paper_strategy_hints ?? 0)}</strong><span>论文RAG策略提示</span></div>
  </div>`;
  const findingsHtml = (findings.length || reviewer.length)
    ? `<div class="graph-findings">
        ${findings.length ? `<h4>证据合成质量门</h4>${findings.map((row) => `<div class="finding-row"><span class="sev ${esc(row.severity)}">${esc(row.severity)}</span><span>${esc(row.message)}</span></div>`).join('')}` : ''}
        ${reviewer.length ? `<h4>Reviewer 阻断/主要发现</h4>${reviewer.map((row) => `<div class="finding-row"><span class="sev ${esc(row.severity)}">${esc(row.severity)}</span><span>${esc(row.message)}</span></div>`).join('')}` : ''}
      </div>`
    : '';
  const linksHtml = links.length
    ? `<div class="graph-findings"><h4>模式跨层链接（假设性，非当前疾病证据）</h4>
        ${links.map((row) => `<div class="pattern-link-row"><span>${esc(row.gene)} · ${esc(row.link_type)}</span><span><b>${esc(row.pattern_name)}</b>（${esc(row.source_lane)} → ${esc(row.target_lane)}）<small>${esc(row.why_this_link)}</small></span></div>`).join('')}
      </div>`
    : '';
  return `${statsRow}${controls}${mechanismLegend()}${svg}${chips}${findingsHtml}${linksHtml}`;
}

function filterMechanismEdges(nodes, edges) {
  const visible = new Set(nodes.map((n) => n.id));
  const laneIds = new Set(nodes.filter((n) => n.node_type === 'lane').map((n) => n.id));
  return {
    edges: edges.filter((e) => {
      if (!visible.has(e.source) || !visible.has(e.target)) return false;
      if (!mechanismFilters.lanes && (laneIds.has(e.source) || laneIds.has(e.target))) return false;
      if (e.relation === 'pattern_evidence_link' && !mechanismFilters.patternLinks) return false;
      if (!mechanismFilters.predictedInferred && (e.claim_class === 'PREDICTED' || e.claim_class === 'INFERRED')) return false;
      return true;
    }),
  };
}

function mechanismLegend() {
  const nodeLegend = [
    ['#2f8f6b', '疾病'], ['#2b6f9e', '基因'], ['#7d5ba6', '位点/变异'],
    ['#b7791f', '细胞/组织'], ['#5c9e77', '药物'], ['#4a7ba6', '证据层'],
  ].map(([color, label]) => `<span class="dot" style="background:${color}"></span>${label}`).join(' ');
  const edgeLegend = [
    ['#8ec5ff', 'FACT 实线'], ['#42d392', 'OBSERVED 实线'],
    ['#ffbf69', 'PREDICTED 虚线'], ['#9a7fd4', 'INFERRED 虚线'],
    ['#ff9f43', '模式跨层链接'], ['#ff6b7a', '安全性阻断'],
  ].map(([color, label]) => `<span style="color:${color}">${label}</span>`).join(' ');
  return `<div class="graph-legend"><b>节点：</b>${nodeLegend}<b>边：</b>${edgeLegend}</div>`;
}

function mechanismLaneChips(laneCoverage, rankedGenes) {
  const coverage = laneCoverage || {};
  const genes = Object.keys(coverage);
  if (!genes.length) return '';
  const laneLabel = { genetics: '遗传学', omics: '组学', perturbation: '扰动', drug: '药物', literature: '文献', safety: '安全' };
  const rows = genes.map((gene) => {
    const lanes = Object.entries(coverage[gene] || {});
    const ranked = rankedGenes.includes(gene);
    const chips = lanes.map(([lane, ids]) => `<span class="lane-chip">${esc(laneLabel[lane] || lane)} ${ids.length}</span>`).join('');
    return `<div style="margin:3px 0"><b style="${ranked ? 'color:var(--cyan)' : ''}">${esc(gene)}${ranked ? ' · 已入排名' : ''}</b> ${chips}</div>`;
  }).join('');
  return `<div class="graph-findings"><h4>每个基因的证据层覆盖</h4>${rows}</div>`;
}

function mechanismSvg(nodes, edges) {
  const colOf = { variant: 0, locus: 0, gene: 1, cell_state: 2, drug: 2, program: 2, trait: 2, lane: 3, disease: 4 };
  const cols = [[], [], [], [], []];
  nodes.forEach((n) => {
    const col = colOf[n.node_type] ?? 2;
    cols[col].push(n);
  });
  const colW = 200, rowH = 56, nodeW = 158, nodeH = 34, padX = 26, padY = 22;
  const maxRows = Math.max(1, ...cols.map((col) => col.length));
  const width = cols.length * colW + padX * 2;
  const height = maxRows * rowH + padY * 2 + 30;
  const pos = new Map();
  cols.forEach((col, colIndex) => {
    col.forEach((n, rowIndex) => {
      const y = ((maxRows - col.length) / 2) * rowH + rowIndex * rowH + padY;
      pos.set(n.id, { x: padX + colIndex * colW, y });
    });
  });
  const nodeColor = {
    disease: '#2f8f6b', gene: '#2b6f9e', locus: '#7d5ba6', variant: '#7d5ba6',
    cell_state: '#b7791f', drug: '#5c9e77', lane: '#4a7ba6', program: '#5c6f9e', trait: '#5c6f9e',
  };
  const edgeStyle = (e) => {
    if (e.relation === 'safety_liability') return { stroke: '#ff6b7a', dash: '' };
    if (e.relation === 'pattern_evidence_link') return { stroke: '#ff9f43', dash: '7 4' };
    if (e.relation === 'known_drug_link') return { stroke: '#5c9e77', dash: '' };
    if (e.relation === 'context_localization') return { stroke: '#b7791f', dash: '' };
    if (['evidence_lane_supports_disease', 'disease_context_relevance', 'colocalization_shared_signal_hypothesis'].includes(e.relation)) return { stroke: '#9a7fd4', dash: '7 4' };
    if (e.claim_class === 'FACT') return { stroke: '#8ec5ff', dash: '' };
    if (e.claim_class === 'OBSERVED') return { stroke: '#42d392', dash: '' };
    if (e.claim_class === 'PREDICTED') return { stroke: '#ffbf69', dash: '5 4' };
    return { stroke: '#9a7fd4', dash: '5 4' };
  };
  const edgeSvg = edges.map((e) => {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return '';
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2;
    const x2 = b.x, y2 = b.y + nodeH / 2;
    const style = edgeStyle(e);
    const title = `${esc(e.relation)} · ${esc(e.claim_class)} · ${(e.evidence_ids || []).length} 条证据`;
    return `<g><title>${title}</title><line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${style.stroke}" stroke-width="1.5" stroke-dasharray="${style.dash}" marker-end="url(#arrow)"/></g>`;
  }).join('');
  const nodeSvg = nodes.map((n) => {
    const posNode = pos.get(n.id);
    if (!posNode) return '';
    const fill = nodeColor[n.node_type] || '#5c6f9e';
    const subtitle = n.node_type === 'cell_state' ? (n.attributes && n.attributes.layer) : n.node_type;
    const label = n.label.length > 20 ? n.label.slice(0, 19) + '…' : n.label;
    const title = `${esc(n.node_type)} · ${esc(n.label)}`;
    return `<g transform="translate(${posNode.x},${posNode.y})"><title>${title}</title>
      <rect width="${nodeW}" height="${nodeH}" rx="8" fill="${fill}" opacity="0.92"/>
      <text x="${nodeW / 2}" y="15" text-anchor="middle" fill="#06121f" font-size="11" font-weight="700">${esc(label)}</text>
      <text x="${nodeW / 2}" y="28" text-anchor="middle" fill="#0b1b2e" font-size="8">${esc(subtitle)}</text>
    </g>`;
  }).join('');
  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" style="max-width:100%">
    <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#5f7ea0"/></marker></defs>
    ${edgeSvg}${nodeSvg}</svg>`;
}

async function renderFiles(snap) {
  const host = $('file-tree');
  const projectId = snap.spec.project_id;
  try {
    const page = await api(`/api/projects/${projectId}/files`);
    const rows = page.files || [];
    if (!rows.length) { host.innerHTML = '<p class="muted">项目目录为空</p>'; return; }
    host.innerHTML = rows.map((row) => {
      const icon = (row.media_type.startsWith('text/') || row.media_type === 'application/json') ? '📄' : '📦';
      return `<button class="file-row" data-path="${esc(row.path)}">${icon} <span>${esc(row.path)}</span><small>${row.size_bytes} B</small></button>`;
    }).join('');
    host.querySelectorAll('button.file-row').forEach((button) => {
      button.addEventListener('click', () => previewFile(projectId, button.dataset.path));
    });
  } catch (error) {
    host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

async function previewFile(projectId, path) {
  const host = $('file-preview');
  try {
    const page = await api(`/api/projects/${projectId}/files/preview?path=${encodeURIComponent(path)}`);
    if (page.reason === 'binary_file') {
      host.innerHTML = `<p class="muted">二进制文件，不支持在线预览：${esc(path)}</p>`;
      return;
    }
    const head = page.truncated ? '<span class="muted">已截断</span>' : '';
    host.innerHTML = `<div class="subhead"><h3>${esc(path)}</h3>${head}</div><pre class="file-preview-content">${esc(page.content)}</pre>`;
  } catch (error) {
    host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}

