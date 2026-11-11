/* 侵权线索内网工作台 v1 单页应用（无外部依赖） */
'use strict';

const state = {
  me: null,
  csrf: '',
  view: 'leads',
  filters: { status: '待复核', platform_group: 'all', tier: 'all', min_amount: '0', ip_code: '', date_from: '', date_to: '', q: '', sort: 'date_desc', page: 1, page_size: 20 },
  leads: { items: [], total: 0, stats: { tiers: {} } },
  selected: new Set(),
  tasks: [],
  ips: [],
  pollTimer: null,
  whitelistLoaded: false,
  viewEpoch: 0,
  leadRequest: 0,
  detailRequest: 0,
  taskRequest: 0,
  previewRequest: 0,
  searchTimer: null,
  taskDraft: null,
  whitelistIp: '',
  taskBoundIp: '',
  reviewer: '',
  taskSubmitting: false,
  lastTask: '',
  version: '',
  accountsTab: 'users',
  settingsTab: 'mail',
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const role = () => (state.me && state.me.role) || 'user';
const isAdmin = () => role() === 'admin' || role() === 'supervisor';
const isSupervisor = () => role() === 'supervisor';
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (n) => n == null || n === '' ? '待核验' : '¥' + Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dateText = (s) => String(s || '').replace('T', ' ');
const compactMoney = (n) => Math.abs(Number(n || 0)) >= 10000 ? '¥' + (Number(n) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + '万' : money(n);
const statusText = (s) => s === '驳回' ? '误报' : s === '已录入SaaS' ? '已录入 SaaS' : s;

function navIcon(key) {
  const paths = {
    leads: '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="m8 12 3 3 5-6"/>',
    tasks: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4Z"/>',
    ips: '<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    accounts: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5"/>',
    settings: '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="9" cy="18" r="2"/>',
    update: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
    logs: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8m-8 4h8m-8 4h5"/>',
  };
  return `<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[key] || paths.settings}</svg>`;
}

function setMenu(open) {
  $('.sidebar').classList.toggle('open', open);
  $('#menu-toggle').setAttribute('aria-expanded', String(open));
  $('#menu-toggle').setAttribute('aria-label', open ? '关闭导航' : '展开导航');
  $('#nav-scrim').hidden = !open;
  $('.canvas').inert = open && matchMedia('(max-width:680px)').matches;
  if (open) $('.sidebar button[data-nav]').focus();
}

function labelTables(scope = $('#main')) {
  scope.querySelectorAll('table').forEach(table => {
    const labels = [...table.querySelectorAll('thead th')].map(el => el.textContent.trim());
    table.querySelectorAll('tbody tr').forEach(row => [...row.cells].forEach((cell, i) => {
      if (cell.colSpan === 1) cell.dataset.label = labels[i] || '操作';
    }));
  });
}

function safeUrl(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch (err) { return ''; }
}

async function withBusy(button, label, work) {
  if (!button || button.disabled) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  button.setAttribute('aria-busy', 'true');
  try { return await work(); }
  finally { button.disabled = false; button.textContent = original; button.removeAttribute('aria-busy'); }
}

function reviewMessage(data) {
  const updated = (data.updated || []).length, skipped = data.skipped || [], missing = data.missing || [];
  return `已更新 ${updated} 条` + (skipped.length ? `，跳过 ${skipped.length} 条（${[...new Set(skipped.map(s => s.reason))].join('、')}）` : '') + (missing.length ? `，未找到 ${missing.length} 条` : '');
}

function toast(text, ok = false) {
  const dialog = $('dialog[open]');
  let wrap = dialog?.querySelector('.dialog-toasts') || $('#toasts');
  if (dialog && wrap === $('#toasts')) {
    wrap = document.createElement('div'); wrap.className = 'dialog-toasts'; wrap.setAttribute('role','status'); wrap.setAttribute('aria-live','polite'); dialog.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts, { headers }));
  if (res.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  let body = {};
  try { body = await res.json(); } catch (e) { body = {}; }
  if (!res.ok) throw new Error(body.error || ('请求失败 ' + res.status));
  return body;
}

function navItems() {
  const items = [
    { key: 'leads', label: '线索复核', roles: ['supervisor', 'admin', 'user'] },
    { key: 'tasks', label: '运行任务', roles: ['supervisor', 'admin', 'user'] },
  ];
  if (isAdmin()) {
    items.push({ key: 'ips', label: 'IP / 白名单', roles: ['supervisor', 'admin'] });
    items.push({ key: 'accounts', label: '账号 / 邀请', roles: ['supervisor', 'admin'] });
    items.push({ key: 'settings', label: '系统设置', roles: ['supervisor', 'admin'] });
    items.push({ key: 'update', label: '版本更新', roles: ['supervisor', 'admin'] });
    items.push({ key: 'logs', label: '审计 / 邮件', roles: ['supervisor', 'admin'] });
  }
  items.push({ key: 'account', label: '修改密码', roles: ['supervisor', 'admin', 'user'] });
  return items;
}

function renderShell() {
  $('#user-name').textContent = state.me.username;
  $('#user-role').textContent = state.me.role_label;
  $('#version').textContent = state.version ? 'v' + state.version.replace(/^v/, '') : '内网版';
  const groups = [['日常工作', ['leads', 'tasks']], ['业务管理', ['ips']], ['系统管理', ['accounts', 'settings', 'update', 'logs']]];
  $('#nav').innerHTML = groups.map(([label, keys]) => {
    const items = navItems().filter(item => keys.includes(item.key));
    return items.length ? `<div class="nav-group"><div class="nav-label">${label}</div>${items.map(item =>
      `<button data-nav="${item.key}" ${state.view === item.key ? 'aria-current="page"' : ''} class="${state.view === item.key ? 'active' : ''}">${navIcon(item.key)}<span>${esc(item.label)}</span><span class="nav-mark" aria-hidden="true">${state.view === item.key ? '›' : ''}</span></button>`).join('')}</div>` : '';
  }).join('');
  $$('[data-nav]').forEach((btn) => btn.onclick = () => { setMenu(false); navigate(btn.dataset.nav); });
  $('#breadcrumb').textContent = (['leads','tasks'].includes(state.view) ? '工作区' : state.view === 'account' ? '个人账户' : '管理') + ' / ' + (navItems().find(i => i.key === state.view)?.label || '线索复核');
  $('#today').textContent = new Date().toLocaleDateString('zh-CN', { year:'numeric', month:'long', day:'numeric' });
  $('#logout').onclick = async () => {
    try { await api('/api/logout', { method: 'POST' }); } catch (e) {}
    location.href = '/login';
  };
}

function navigate(view) {
  if (state.view === 'tasks' && $('#t-ip')) saveTaskDraft();
  if ($('#modal-root [aria-busy="true"]') || $('#drawer-root [aria-busy="true"]')) return;
  clearTimeout(state.pollTimer);
  clearTimeout(state.searchTimer);
  closeDrawer(); closeModal();
  state.viewEpoch++;
  state.view = view;
  state.selected.clear();
  history.replaceState(null, '', '#' + view);
  document.title = (navItems().find(i => i.key === view)?.label || '线索复核') + ' · 侵权线索工作台';
  $('#main').innerHTML = '<div class="empty" role="status">正在加载页面…</div>';
  renderShell();
  const renderers = {
    leads: renderLeads, tasks: renderTasks, ips: renderIps, accounts: renderAccounts,
    settings: renderSettings, update: renderUpdate, logs: renderLogs, account: renderAccount,
  };
  const fn = renderers[view] || renderLeads;
  const epoch = state.viewEpoch;
  Promise.resolve(fn()).then(() => { if (epoch === state.viewEpoch) labelTables(); }).catch((err) => {
    if (epoch !== state.viewEpoch) return;
    toast(err.message);
    if (!$('#main').querySelector('.page-head')) {
      $('#main').innerHTML = '<div class="card empty"><h3>页面暂时无法加载</h3><p>请检查连接后重试。</p><button class="btn" id="retry-view">重新加载</button></div>';
      $('#retry-view').onclick = () => navigate(view);
    }
  });
}

/* ------------------------------------------------------------------ */
/* 线索 */
function renderLeads() {
  const f = state.filters;
  $('#main').innerHTML = `
    <div class="page-head">
      <div><div class="eyebrow">复核工作区</div><h2>线索复核</h2><div class="desc">筛选重点店铺，核对命中依据，完成复核与录入。</div></div>
      <div class="row"><button class="btn" id="export-btn">导出今日 Excel</button><button class="btn primary" id="refresh-leads">刷新线索</button></div>
    </div>
    <div class="card leads-panel">
    <div class="status-tabs" aria-label="线索状态">
      ${[['待复核','待复核'],['已确认','已确认'],['驳回','误报'],['已录入SaaS','已录入 SaaS'],['all','全部线索']].map(([v,l]) => `<button class="status-tab ${f.status===v?'active':''}" data-status="${v}" aria-pressed="${f.status===v}">${l}</button>`).join('')}
    </div>
    <div class="filter-card">
      <div class="row">
        <label class="field grow">搜索<input id="f-q" value="${esc(f.q)}" placeholder="店铺名 / 线索编号 / 命中规则"></label>
        <label class="field">平台<select id="f-platform">
          ${[['all','全部平台'],['taobao','淘宝/天猫'],['jd','京东'],['1688','1688']].map(([v,l]) => `<option value="${v}" ${f.platform_group===v?'selected':''}>${l}</option>`).join('')}
        </select></label>
        <label class="field">风险<select id="f-tier">
          ${[['all','全部'],['达标','达标'],['待核验','待核验'],['临界','临界']].map(([v,l]) => `<option value="${v}" ${f.tier===v?'selected':''}>${l}</option>`).join('')}
        </select></label>
        <label class="field">排序<select id="f-sort">
          ${[['date_desc','日期新→旧'],['amount_desc','金额高→低'],['confidence_desc','置信度高→低'],['date_asc','日期旧→新']].map(([v,l]) => `<option value="${v}" ${f.sort===v?'selected':''}>${l}</option>`).join('')}
        </select></label>
        <label class="field">预估金额<select id="f-amount">
          ${[['0','不限制'],['50000','5万元以上'],['100000','10万元以上'],['200000','20万元以上'],['400000','40万元以上'],['500000','50万元以上']].map(([v,l]) => `<option value="${v}" ${f.min_amount===v?'selected':''}>${l}</option>`).join('')}
        </select></label>
      </div>
      <div class="filter-footer"><details ${f.ip_code||f.date_from||f.date_to?'open':''}><summary>更多筛选<span class="muted"> · IP 与日期</span></summary><div class="grid3 advanced-fields">
        <label class="field">IP 代码<input id="f-ip" value="${esc(f.ip_code)}" placeholder="如 huluxiongdi"></label>
        <label class="field">开始日期<input id="f-date-from" type="date" value="${esc(f.date_from)}"></label>
        <label class="field">结束日期<input id="f-date-to" type="date" value="${esc(f.date_to)}"></label>
      </div></details><button class="btn small" id="reset-filters">重置筛选</button></div>
      <div id="filter-error" class="inline-error hidden" role="alert"></div>
    </div>
    <div class="result-region">
      <div class="row result-caption">
        <div class="stats" id="lead-stats"></div>
        <div class="row"><span class="muted small" id="page-info"></span></div>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th style="width:26px"><input type="checkbox" id="check-all" aria-label="选择当前页可复核线索"></th><th>店铺 / IP</th><th>平台</th><th class="numeric">预估金额</th><th>风险</th><th>操作</th></tr></thead>
        <tbody id="lead-body"><tr><td colspan="6" class="empty">加载中…</td></tr></tbody>
      </table></div>
      <div class="batch-bar hidden" id="batch-bar"></div>
      <div class="pagination" id="pagination"></div>
    </div></div>`;
  $('#refresh-leads').onclick = () => withBusy($('#refresh-leads'), '刷新中…', loadLeads);
  $('#export-btn').onclick = exportExcel;
  $$('[data-status]').forEach(btn => btn.onclick = () => {
    f.status = btn.dataset.status; f.page = 1;
    $$('[data-status]').forEach(b => { b.classList.toggle('active', b === btn); b.setAttribute('aria-pressed', String(b === btn)); });
    loadLeads();
  });
  $('#reset-filters').onclick = () => {
    clearTimeout(state.searchTimer);
    Object.assign(f, {status:'待复核',platform_group:'all',tier:'all',min_amount:'0',ip_code:'',date_from:'',date_to:'',q:'',sort:'date_desc',page:1});
    renderLeads();
  };
  $('#f-platform').onchange = (e) => { f.platform_group = e.target.value; f.page = 1; loadLeads(); };
  $('#f-tier').onchange = (e) => { f.tier = e.target.value; f.page = 1; loadLeads(); };
  $('#f-amount').onchange = (e) => { f.min_amount = e.target.value; f.page = 1; loadLeads(); };
  $('#f-sort').onchange = (e) => { f.sort = e.target.value; f.page = 1; loadLeads(); };
  $('#f-q').oninput = (e) => { clearTimeout(state.searchTimer); f.q = e.target.value.trim(); f.page = 1; state.leadRequest++; state.searchTimer = setTimeout(loadLeads, 300); };
  $('#f-ip').onchange = (e) => { f.ip_code = e.target.value.trim(); f.page = 1; loadLeads(); };
  $('#f-date-from').onchange = (e) => { f.date_from = e.target.value; f.page = 1; loadLeads(); };
  $('#f-date-to').onchange = (e) => { f.date_to = e.target.value; f.page = 1; loadLeads(); };
  $('#check-all').onchange = (e) => {
    $$('#lead-body input[data-lead]:not(:disabled)').forEach((box) => { if (box.checked = e.target.checked) state.selected.add(box.dataset.lead); else state.selected.delete(box.dataset.lead); });
    renderBatchBar();
  };
  return loadLeads();
}

async function loadLeads() {
  const f = state.filters;
  const request = ++state.leadRequest, epoch = state.viewEpoch;
  const error = $('#filter-error');
  if (f.date_from && f.date_to && f.date_from > f.date_to) {
    error.textContent = '开始日期不能晚于结束日期，请调整后重试。'; error.classList.remove('hidden');
    return;
  }
  error.classList.add('hidden');
  state.selected.clear(); renderBatchBar();
  $('#lead-body').innerHTML = '<tr><td colspan="6" class="empty" role="status">正在查询线索…</td></tr>';
  $('#pagination').innerHTML = '';
  const qs = new URLSearchParams({ status: f.status, platform_group: f.platform_group, tier: f.tier, min_amount: f.min_amount, ip_code: f.ip_code, date_from: f.date_from, date_to: f.date_to, q: f.q, sort: f.sort, page: f.page, page_size: f.page_size });
  let data;
  try { data = await api('/api/leads?' + qs.toString()); }
  catch (err) {
    if (request !== state.leadRequest || epoch !== state.viewEpoch || state.view !== 'leads') return;
    $('#lead-body').innerHTML = '<tr><td colspan="6" class="empty">查询失败，请点击“刷新线索”重试。</td></tr>';
    toast(err.message); return;
  }
  if (request !== state.leadRequest || epoch !== state.viewEpoch || state.view !== 'leads') return;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (f.page > pages) { f.page = pages; return loadLeads(); }
  state.leads = data;
  state.selected = new Set([...state.selected].filter((no) => data.items.some((item) => item.lead_no === no)));
  const stats = data.stats || {};
  $('#lead-stats').innerHTML = `<span>找到 <strong>${data.total}</strong> 条线索</span><span>达标 ${(stats.tiers||{}).达标||0} · 待核验 ${(stats.tiers||{}).待核验||0} · 临界 ${(stats.tiers||{}).临界||0}</span>`;
  const statusCounts = {'待复核':stats.pending, '已确认':stats.confirmed, '驳回':stats.rejected};
  $$('[data-status]').forEach(btn => {
    const count = statusCounts[btn.dataset.status];
    btn.innerHTML = `${esc(btn.dataset.status === 'all' ? '全部线索' : statusText(btn.dataset.status))}${count == null ? '' : `<small>${count}</small>`}`;
  });
  const body = $('#lead-body');
  if (!data.items.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">没有符合条件的线索，请调整或重置筛选。</td></tr>';
  } else {
    body.innerHTML = data.items.map((item) => `
      <tr>
        <td><input type="checkbox" data-lead="${esc(item.lead_no)}" aria-label="选择 ${esc(item.shop_name || item.lead_no)}" ${item.status==='已录入SaaS'?'disabled title="已录入 SaaS，不可覆盖"':''} ${state.selected.has(item.lead_no)?'checked':''}></td>
        <td class="lead-shop"><div class="shop-name">${esc(item.shop_name || '（店名缺失）')}</div><div class="muted small">${esc((item.ip_codes||[]).join('、') || '未标注 IP')} · ${esc(item.snapshot_date)}</div>${f.status==='all'?`<span class="pill status-${esc(item.status)}">${esc(statusText(item.status))}</span>`:''}${item.reviewer?`<div class="muted small">${esc(item.reviewer)} · ${esc((item.reviewed_at||'').slice(0,16))}</div>`:''}</td>
        <td><span class="pill">${esc(item.platform)}</span></td>
        <td class="numeric"><span class="money" title="${money(item.total_amount)}">${compactMoney(item.total_amount)}</span><div class="muted small">${item.link_count} 条链接</div></td>
        <td><span class="pill tier-${esc(item.tier)}">${esc(item.tier)}</span></td>
        <td><button class="btn small" data-open="${esc(item.lead_no)}">${item.status==='已录入SaaS'?'查看详情':'查看并复核'}</button></td>
      </tr>`).join('');
    $$('#lead-body input[data-lead]').forEach((box) => box.onchange = () => {
      if (box.checked) state.selected.add(box.dataset.lead); else state.selected.delete(box.dataset.lead);
      renderBatchBar();
    });
    $$('#lead-body button[data-open]').forEach((btn) => btn.onclick = () => openLead(btn.dataset.open));
  }
  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  $('#page-info').textContent = `第 ${data.page} / ${totalPages} 页`;
  $('#pagination').innerHTML = `<span>每页</span><select id="page-size" aria-label="每页条数">${[20,50,100].map(n=>`<option ${f.page_size===n?'selected':''}>${n}</option>`).join('')}</select><span>条</span><button class="btn small" id="prev-page" ${data.page<=1?'disabled':''}>上一页</button><button class="btn small" id="next-page" ${data.page>=totalPages?'disabled':''}>下一页</button>`;
  $('#page-size').onchange = e => { f.page_size = Number(e.target.value); f.page = 1; loadLeads(); };
  $('#prev-page').onclick = () => { state.filters.page--; loadLeads(); };
  $('#next-page').onclick = () => { state.filters.page++; loadLeads(); };
  renderBatchBar();
  labelTables();
}

function renderBatchBar() {
  const bar = $('#batch-bar');
  if (!bar) return;
  const count = state.selected.size;
  const available = state.leads.items.filter(i => i.status !== '已录入SaaS');
  const checked = available.filter(i => state.selected.has(i.lead_no)).length;
  const all = $('#check-all');
  if (all) { all.checked = available.length > 0 && checked === available.length; all.indeterminate = checked > 0 && checked < available.length; all.disabled = !available.length; }
  bar.classList.toggle('hidden', count === 0);
  if (!count) return;
  bar.innerHTML = `<span>当前页已选 <strong>${count}</strong> 条</span>
    <span class="row"><button class="btn small" id="clear-selection">取消选择</button><label class="field">复核人<input id="batch-reviewer" value="${esc(state.reviewer || state.me.username)}"></label>
    <button class="btn small danger" data-batch="已确认">批量确认侵权</button>
    <button class="btn small" data-batch="驳回">批量标为误报</button>
    <button class="btn small ok" data-batch="已录入SaaS">批量已录入</button></span>`;
  $$('#batch-bar button[data-batch]').forEach((btn) => btn.onclick = () => batchReview(btn.dataset.batch));
  $('#batch-reviewer').oninput = e => { state.reviewer = e.target.value; };
  $('#clear-selection').onclick = () => { state.selected.clear(); $$('#lead-body input[data-lead]').forEach(b => { b.checked = false; }); renderBatchBar(); };
}

function batchReview(result) {
  const reviewer = ($('#batch-reviewer')?.value || state.me.username).trim();
  if (!reviewer) { toast('请填写复核人'); return; }
  const numbers = [...state.selected], epoch = state.viewEpoch;
  if (!numbers.length) return;
  const typed = result === '驳回' && numbers.length > 1;
  showModal(`<h3>确认批量复核</h3><p>将当前页选中的 <strong>${numbers.length}</strong> 条线索标为 <strong>${esc(result==='驳回'?'误报（驳回）':result)}</strong>。</p><p class="help">复核人：${esc(reviewer)}${result==='已录入SaaS'?'。录入后不再允许覆盖复核结果。':''}</p>
    ${typed?'<label class="field">请输入“确认删除”以确认批量驳回<input id="batch-confirm" autocomplete="off"></label><p class="help">此操作标记为误报，保留线索与复核记录。</p>':''}
    <div class="actions"><button class="btn" id="m-cancel">返回</button><button class="btn primary" id="m-apply">确认复核</button></div>`, root => {
    root.querySelector('#m-cancel').onclick = closeModal;
    root.querySelector('#m-apply').onclick = () => withBusy(root.querySelector('#m-apply'), '保存中…', async () => {
      const confirmText = typed ? root.querySelector('#batch-confirm').value.trim() : '';
      if (typed && confirmText !== '确认删除') { toast('请输入“确认删除”以确认批量驳回'); return; }
      try {
        const data = await api('/api/leads/review', { method:'POST', body:JSON.stringify({lead_nos:numbers,result,reviewer,confirm_text:confirmText}) });
        closeModal(true);
        toast(reviewMessage(data), data.updated.length > 0);
        if (epoch === state.viewEpoch) { state.selected.clear(); await loadLeads(); }
      } catch (err) { toast(err.message); }
    });
  });
}

async function openLead(no) {
  const request = ++state.detailRequest, epoch = state.viewEpoch;
  try {
    const data = await api('/api/leads/' + encodeURIComponent(no));
    if (request !== state.detailRequest || epoch !== state.viewEpoch || state.view !== 'leads') return;
    const links = data.links || [];
    const root = $('#drawer-root');
    root.innerHTML = `<dialog class="drawer-dialog" id="drawer-overlay" aria-labelledby="lead-title"><div class="drawer">
      <div class="drawer-head"><div><h3 id="lead-title">${esc(data.shop_name || '线索详情')}</h3><div class="muted small">${esc(data.lead_no)} · ${esc(data.platform)} · ${esc(data.snapshot_date)}</div></div><button class="btn small" id="close-drawer">关闭</button></div>
      <div class="review-grid"><div class="evidence-column">
      <section class="review-summary"><h4>店铺汇总</h4><div class="detail-amount money">${money(data.total_amount)}</div><p class="help">${links.length} 条商品链接 · <span class="pill tier-${esc(data.tier)}">${esc(data.tier)}</span></p><details><summary>档案与命中信息</summary><div class="kv">
        <span class="k">合计金额</span><span class="money">${money(data.total_amount)}</span>
        <span class="k">风险等级</span><span><span class="pill tier-${esc(data.tier)}">${esc(data.tier)}</span></span>
        <span class="k">店铺类型</span><span>${esc(data.shop_type)}</span>
        <span class="k">平台 / IP</span><span>${esc(data.platform)} · ${esc((data.ip_codes||[]).join('、'))}</span>
        <span class="k">命中原因</span><span>${esc(data.hit_summary)}</span>
        <span class="k">当前状态</span><span><span class="pill status-${esc(data.status)}">${esc(data.status)}</span> ${esc(data.reviewer||'')}</span>
        <span class="k">缺失标记</span><span>${esc((data.missing_flags||[]).join('、') || '无')}</span>
      </div></details></section>
      <section><h4>链接明细（${links.length}）</h4><div class="links">${links.map((link) => `
        <div class="link-item"><div class="title">${esc(link.title)}</div>
          <div class="muted small">${esc(link.formula)} · ${money(link.amount)}</div>
          <div class="muted small">文字 ${esc(link.text_score)} / 视觉 ${esc(link.visual_score)} / 最终 ${esc(link.final_score)}</div>
          <div class="row" style="margin-top:6px">${safeUrl(link.url)?`<a class="btn small" href="${esc(safeUrl(link.url))}" target="_blank" rel="noopener noreferrer">打开原链接 ↗</a>`:'<span class="muted small">原链接不可用</span>'}<span class="muted small">${esc(link.shop_name)}</span></div>
        </div>`).join('') || '<div class="muted small">没有链接明细</div>'}</div></section>
      </div><section class="review-section"><h4>人工复核</h4><p class="help">核对左侧证据后，选择复核结论。</p>${data.status==='已录入SaaS'?'<div class="notice">该线索已录入 SaaS，复核记录受保护，可继续查看链接依据。</div>':`<div class="review-box">
        <div class="radio-row">
          ${['已确认','驳回','已录入SaaS'].map((v) => `<label class="review-choice"><input type="radio" name="review-result" value="${v}" ${data.status===v?'checked':''}> ${v==='已确认'?'确认侵权':v==='驳回'?'标记误报（驳回）':'已录入 SaaS'}</label>`).join('')}
        </div>
        <label class="field">复核人<input id="reviewer" value="${esc(state.reviewer || data.reviewer || state.me.username)}"></label>
        <label class="check continue-review"><input id="review-next" type="checkbox" checked> 保存后继续下一条待复核线索</label>
        <div class="row" style="margin-top:12px"><button class="btn primary" id="submit-review">保存复核结果</button><span class="muted small">复核记录将自动保存。</span></div>
      </div>`}</section></div>
    </div></dialog>`;
    const dialog = $('#drawer-overlay');
    dialog.showModal();
    dialog.oncancel = e => { e.preventDefault(); closeDrawer(); };
    $('#close-drawer').onclick = closeDrawer;
    dialog.onclick = (e) => { if (e.target === dialog) closeDrawer(); };
    if (!$('#submit-review')) return;
    $('#submit-review').onclick = () => withBusy($('#submit-review'), '保存中…', async () => {
      const picked = document.querySelector('input[name="review-result"]:checked');
      if (!picked) { toast('请选择复核结论'); return; }
      const reviewer = ($('#reviewer').value || '').trim();
      if (!reviewer) { toast('请填写复核人'); return; }
      const next = $('#review-next').checked;
      state.reviewer = reviewer;
      const nextNo = state.leads.items.find(item => item.lead_no !== no && item.status === '待复核')?.lead_no;
      try {
        const result = await api('/api/leads/review', { method: 'POST', body: JSON.stringify({ lead_nos: [data.lead_no], result: picked.value, reviewer }) });
        closeDrawer(true);
        toast(reviewMessage(result), result.updated.length > 0);
        if (epoch !== state.viewEpoch) return;
        await loadLeads();
        if (next && result.updated.length) {
          const candidate = state.leads.items.find(i => i.lead_no === nextNo && i.status === '待复核') || state.leads.items.find(i => i.lead_no !== no && i.status === '待复核');
          if (candidate) await openLead(candidate.lead_no);
        }
      } catch (err) { toast(err.message); }
    });
  } catch (err) { if (request === state.detailRequest && epoch === state.viewEpoch) toast(err.message); }
}
function closeDrawer(force = false) {
  const dialog = $('#drawer-root dialog');
  if (force !== true && dialog?.querySelector('[aria-busy="true"]')) return;
  state.detailRequest++;
  if (dialog) dialog.close();
  $('#drawer-root').innerHTML = '';
}

async function exportExcel() {
  return withBusy($('#export-btn'), '生成中…', async () => { try {
    const data = await api('/api/export', { method: 'POST', body: JSON.stringify({}) });
    toast('Excel 已生成：' + data.path, true);
  } catch (err) { toast(err.message); } });
}

/* ------------------------------------------------------------------ */
/* 任务 */
async function renderTasks() {
  const epoch = state.viewEpoch;
  const [tasks, ips] = await Promise.all([api('/api/tasks'), api('/api/ips')]);
  if (epoch !== state.viewEpoch || state.view !== 'tasks') return;
  state.tasks = tasks.items; state.ips = ips.items;
  const ipOptions = state.ips.map((ip) => `<option value="${esc(ip.name)}"></option>`).join('');
  $('#main').innerHTML = `
    <div class="page-head"><div><div class="eyebrow">采集工作区</div><h2>运行任务</h2><div class="desc">跟进采集进度，查看结果与本次报表。</div></div><button class="btn primary" id="create-task">＋ 新建任务</button></div>
    <div class="task-metrics" id="task-metrics"></div>
    <template id="task-template"><div class="task-layout"><form class="card task-form" id="task-form">
      <fieldset id="task-fields" ${state.taskSubmitting?'disabled':''}>
      <div class="section-heading"><span class="step-number">01</span><div><h3>采集范围</h3><p class="help">选择 IP 和采集平台。</p></div></div>
      <div class="grid2">
        <label class="field"><span class="field-title">IP <span class="required">必填</span></span><input id="t-ip" list="ip-list" required maxlength="200" placeholder="输入或选择 IP"><datalist id="ip-list">${ipOptions}</datalist></label>
        <label class="field"><span class="field-title">运行模式</span><select id="t-mode"><option value="formal">立即运行</option><option value="dry_run">采集演练</option></select></label>
      </div>
      <div class="preview-line"><button type="button" class="btn small" id="preview-ip">加载档案</button><span class="help" id="preview-hint">已有 IP 请先读取；新 IP 将自动建档。</span></div>
      <fieldset class="platform-field"><legend>采集平台 <span class="required">至少选一项</span></legend>
          <div class="platform-options">
             <label class="check"><input type="checkbox" class="t-platform" value="taobao" checked> 淘宝/天猫</label>
             <label class="check"><input type="checkbox" class="t-platform" value="jd" checked> 京东</label>
             <label class="check"><input type="checkbox" class="t-platform" value="1688" checked> 1688</label>
          </div>
      </fieldset>
      <div class="section-heading"><span class="step-number">02</span><div><h3>执行与提醒</h3><p class="help">任务创建后立即运行。</p></div></div>
      <div class="grid2">
        <label class="field"><span class="field-title">常规采集截止时间 <span class="optional">选填</span></span><input id="t-ends-at" type="datetime-local"><span class="help">留空不限时，逐页采集到末页。仅在填写后按时停止常规采集；补采收尾后生成报表。</span></label>
        <label class="field"><span class="field-title">提醒邮箱 <span class="optional">选填</span></span><input id="t-email" type="email"><span class="help">整体任务结束后按配置发送通知，补采等待期间不发送。</span></label>
      </div>
      <details class="advanced-settings" id="task-advanced"><summary>高级设置 <span class="muted">· 关键词、公司、白名单</span></summary>
      <label class="field">采集关键词<input id="t-keywords" placeholder="多个词用逗号分隔；留空使用 IP 名"></label>
      <div class="grid2 advanced-fields">
        <label class="field">授权公司代码<input id="t-company" placeholder="如 company-a"></label>
        <label class="field">公司名称<input id="t-company-name"></label>
      </div>
      <label class="check company-check"><input type="checkbox" id="t-include-company" checked> 并入该公司的共享白名单</label>
      <h4>白名单</h4><p class="help">按 IP 的条目可编辑；已加载的公司共享条目只读。更改会随本次任务保存。</p>
      <div id="wl-rows"></div>
      <button type="button" class="btn small" id="add-wl">+ 添加白名单店铺</button>
      </details>
      </fieldset>
      <div class="task-submit"><button type="submit" class="btn primary" id="submit-task" ${state.taskSubmitting?'disabled':''}>${state.taskSubmitting?'提交中…':'检查并运行'}</button><span class="help" id="task-hint" role="status">草稿会自动保留</span></div><div id="task-confirmation" hidden></div>
    </form>
    <aside class="card run-summary"><div class="eyebrow">运行摘要</div><h3 id="summary-ip">尚未选择 IP</h3><div id="task-summary"></div><div class="notice">正式运行生成线索；演练不写入线索。</div><p class="help">已有任务运行时，需等待结束后再创建。</p></aside></div></template>
    <div class="card">
      <div class="row record-heading"><div><h3>任务记录</h3><span class="help" id="task-refresh-state" role="status"></span></div><button class="btn small" id="refresh-tasks">刷新记录</button></div>
      <div class="table-wrap"><table><thead><tr><th>任务 / IP</th><th>平台</th><th>状态</th><th>候选线索</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="task-body"></tbody></table></div>
    </div>
    <div class="card" id="legacy-scheduled" hidden><h3 style="margin:0 0 8px">历史预约任务</h3><div id="scheduled-list" class="muted small">加载中…</div></div>`;
  $('#create-task').onclick = openTaskForm;
  $('#refresh-tasks').onclick = () => withBusy($('#refresh-tasks'), '刷新中…', refreshTasks);
  renderTaskTable();
  await loadScheduled();
  if (state.tasks.some((t) => ['running','queued','scheduled'].includes(t.status))) schedulePoll();
}

function openTaskForm() {
  const content = $('#task-template').content.cloneNode(true);
  showModal('<div class="dialog-heading"><div><div class="eyebrow">采集任务</div><h3>新建任务</h3></div><button class="btn small" id="close-task">保存并关闭</button></div><div id="task-form-mount"></div>', root => {
    root.querySelector('#modal-overlay').classList.add('wide-dialog');
    root.querySelector('#task-form-mount').appendChild(content);
    root.querySelector('#close-task').onclick = closeModal;
  });
  $('#preview-ip').onclick = previewIp;
  $('#add-wl').onclick = () => addWlRow({});
  $('#task-form').onsubmit = e => { e.preventDefault(); submitTask(); };
  restoreTaskDraft();
  $('#t-ip').oninput = () => { invalidateIp(); saveTaskDraft(); updateTaskSummary(); };
  $('#task-form').oninput = () => { saveTaskDraft(); updateTaskSummary(); };
  $('#task-form').onchange = () => { saveTaskDraft(); updateTaskSummary(); };
  updateTaskSummary();
}

const taskFieldIds = ['t-ip','t-company','t-company-name','t-mode','t-email','t-keywords','t-ends-at'];
function saveTaskDraft() {
  if (!$('#task-form') || state.view !== 'tasks') return;
  state.taskDraft = {
    values: Object.fromEntries(taskFieldIds.map(id => [id, $('#' + id).value])),
    platforms: $$('.t-platform:checked').map(el => el.value), includeCompany: $('#t-include-company').checked,
    whitelist: whitelistRows(), loaded: state.whitelistLoaded, ip: state.whitelistIp,
    advanced: $('#task-advanced').open,
  };
}
function restoreTaskDraft() {
  const draft = state.taskDraft;
  state.whitelistLoaded = draft?.loaded || false;
  state.whitelistIp = draft?.ip || '';
  if (draft) {
    taskFieldIds.forEach(id => { $('#' + id).value = draft.values[id] || ''; });
    $$('.t-platform').forEach(el => { el.checked = draft.platforms.includes(el.value); });
    $('#t-include-company').checked = draft.includeCompany;
    $('#task-advanced').open = draft.advanced;
    draft.whitelist.forEach(addWlRow);
  } else addWlRow({});
  state.taskBoundIp = $('#t-ip').value.trim();
  if (state.whitelistLoaded) $('#preview-hint').textContent = '已加载档案：' + state.whitelistIp;
}
function invalidateIp() {
  const name = $('#t-ip').value.trim();
  if (name === state.taskBoundIp) return;
  state.taskBoundIp = name;
  state.previewRequest++;
  state.whitelistLoaded = false; state.whitelistIp = '';
  ['#t-company','#t-company-name','#t-keywords'].forEach(id => { $(id).value = ''; });
  $('#wl-rows').innerHTML = ''; addWlRow({});
  $('#preview-hint').textContent = 'IP 已切换，请重新加载对应的档案与白名单。';
}
function updateTaskSummary() {
  if (!$('#task-summary') || state.view !== 'tasks') return;
  const name = $('#t-ip').value.trim(), endsAt = $('#t-ends-at').value;
  $('#summary-ip').textContent = name || '尚未选择 IP';
  const labels = {taobao:'淘宝/天猫',jd:'京东','1688':'1688'};
  const platforms = $$('.t-platform:checked').map(el => labels[el.value]);
  $('#task-summary').innerHTML = `<dl class="summary-list"><dt>采集平台</dt><dd>${esc(platforms.join('、') || '尚未选择')}</dd><dt>运行模式</dt><dd>${$('#t-mode').value==='dry_run'?'采集演练':'立即运行'}</dd><dt>开始时间</dt><dd>立即运行</dd><dt>常规采集截止时间</dt><dd>${endsAt?esc(dateText(endsAt)):'不限时，逐页采集到末页'}</dd><dt>完成提醒</dt><dd>${esc($('#t-email').value.trim() || '不发送邮件')}</dd><dt>白名单</dt><dd>${state.whitelistLoaded?'已加载档案':collectWhitelist().length?'使用当前编辑条目':'沿用已有配置'}${$('#t-include-company').checked?' · 包含公司共享':''}</dd></dl>`;
  if (!state.taskSubmitting) $('#submit-task').textContent = '检查并运行';
}

async function refreshTasks() {
  const epoch = state.viewEpoch, request = ++state.taskRequest;
  try {
    const data = await api('/api/tasks');
    if (epoch !== state.viewEpoch || request !== state.taskRequest || state.view !== 'tasks') return;
    state.tasks = data.items;
    renderTaskTable(); await loadScheduled();
    if (epoch !== state.viewEpoch || state.view !== 'tasks') return;
    $('#task-refresh-state').textContent = '已更新 ' + new Date().toLocaleTimeString('zh-CN');
    if (state.tasks.some(t => ['running','queued','scheduled'].includes(t.status))) schedulePoll();
    else clearTimeout(state.pollTimer);
  } catch (err) {
    if (epoch !== state.viewEpoch || state.view !== 'tasks') return;
    $('#task-refresh-state').textContent = '更新失败，稍后自动重试；也可手动刷新。';
    toast(err.message);
    schedulePoll(10000);
  }
}

function addWlRow(entry) {
  const wrap = $('#wl-rows');
  if (!wrap) return;
  const scope = entry.scope_type || 'ip';
  const readonly = scope === 'company';
  const row = document.createElement('div');
  row.className = 'wl-row';
  row.dataset.readonly = String(readonly);
  row.innerHTML = `
    <select class="wl-scope" aria-label="白名单范围" disabled><option value="${readonly?'company':'ip'}">${readonly?'公司共享':'当前 IP'}</option></select>
    <select class="wl-platform" aria-label="白名单平台" ${readonly?'disabled':''}><option value="taobao" ${entry.platform_group==='taobao'?'selected':''}>淘宝/天猫</option><option value="jd" ${entry.platform_group==='jd'?'selected':''}>京东</option><option value="1688" ${entry.platform_group==='1688'?'selected':''}>1688</option></select>
    <input class="wl-name" aria-label="白名单店铺名" placeholder="店铺名" value="${esc(entry.shop_name || '')}" ${readonly?'readonly':''}>
    <input class="wl-id" aria-label="白名单店铺 ID" placeholder="店铺ID（选填）" value="${esc(entry.shop_id || '')}" ${readonly?'readonly':''}>
    <input class="wl-note" aria-label="白名单备注" placeholder="备注" value="${esc(entry.note || '')}" ${readonly?'readonly':''}>
    <button class="del" type="button" aria-label="移除白名单条目" ${readonly?'disabled':''}>×</button>`;
  row.querySelector('.del').onclick = () => { row.remove(); saveTaskDraft(); updateTaskSummary(); };
  wrap.appendChild(row);
}

function whitelistRows() {
  return $$('#wl-rows .wl-row').map((row) => ({
    scope_type: row.querySelector('.wl-scope').value,
    platform_group: row.querySelector('.wl-platform').value,
    shop_name: row.querySelector('.wl-name').value.trim(),
    shop_id: row.querySelector('.wl-id').value.trim(),
    note: row.querySelector('.wl-note').value.trim(),
  }));
}
function collectWhitelist() {
  return whitelistRows().filter(item => item.shop_name && item.scope_type === 'ip');
}

async function previewIp() {
  const name = $('#t-ip').value.trim();
  if (!name) { toast('请先输入 IP 名'); return; }
  const epoch = state.viewEpoch, request = ++state.previewRequest;
  return withBusy($('#preview-ip'), '加载中…', async () => { try {
    const data = await api('/api/tasks/preview-ip', { method: 'POST', body: JSON.stringify({ name }) });
    if (request !== state.previewRequest || epoch !== state.viewEpoch || state.view !== 'tasks' || !$('#t-ip') || $('#t-ip').value.trim() !== name) return;
    const ip = data.ip || {};
    $('#t-company').value = ip.company_code || '';
    $('#t-company-name').value = ip.company_name || '';
    $('#t-keywords').value = (ip.keywords || []).join(',');
    $('#wl-rows').innerHTML = '';
    (data.whitelist || []).forEach((entry) => addWlRow(entry));
    if (!(data.whitelist || []).length) addWlRow({});
    state.whitelistLoaded = true;
    state.whitelistIp = name;
    state.taskBoundIp = name;
    $('#preview-hint').textContent = ip.id ? `已带出档案：${ip.name}（${(data.whitelist||[]).length} 条白名单）` : '新 IP，提交任务时会自动建档。';
    toast('已带出 IP 档案', true);
    saveTaskDraft(); updateTaskSummary();
  } catch (err) { if (epoch === state.viewEpoch) toast(err.message); } });
}

async function submitTask() {
  if (state.taskSubmitting || $('#preview-ip').disabled) return;
  if (state.tasks.some(t => ['running','queued'].includes(t.status))) { toast('已有任务在运行，请等待它结束；本次草稿已保留'); return; }
  const ipName = $('#t-ip').value.trim();
  if (!ipName) { toast('IP 名不能为空'); return; }
  const platforms = $$('.t-platform:checked').map((box) => box.value);
  if (!platforms.length) { toast('至少选择一个平台'); return; }
  if (!$('#t-email').reportValidity()) return;
  const endsAt = $('#t-ends-at').value;
  if (endsAt && (!Number.isFinite(new Date(endsAt).getTime()) || new Date(endsAt) <= new Date())) { toast('常规采集截止时间必须晚于当前时间'); $('#t-ends-at').focus(); return; }
  if (state.ips.some(ip => ip.name === ipName) && (!state.whitelistLoaded || state.whitelistIp !== ipName)) { toast('请先加载该 IP 的档案与白名单，避免覆盖已有配置'); $('#preview-ip').focus(); return; }
  if (whitelistRows().some(item => !item.shop_name && (item.shop_id || item.note))) { toast('白名单条目填写了 ID 或备注，请补全店铺名'); $('#task-advanced').open = true; return; }
  const payload = {
    ip_name: ipName,
    company_code: $('#t-company').value.trim(),
    company_name: $('#t-company-name').value.trim(),
    platforms,
    mode: $('#t-mode').value,
    notify_email: $('#t-email').value.trim(),
    keywords: $('#t-keywords').value.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean),
    include_company: $('#t-include-company').checked,
    ends_at: endsAt,
    whitelist: collectWhitelist(),
    whitelist_provided: (state.whitelistLoaded && state.whitelistIp === ipName) || collectWhitelist().length > 0,
  };
  $('#task-fields').hidden = true;
  $('.task-submit').hidden = true;
  const confirmation = $('#task-confirmation');
  confirmation.hidden = false;
  confirmation.innerHTML = `<h3>确认本次运行</h3><p>即将为 <strong>${esc(ipName)}</strong> 创建${payload.mode==='dry_run'?'采集演练':'正式采集'}任务，创建后立即开始。</p><p class="help">平台：${esc(platforms.join('、'))}<br>常规采集截止时间：${endsAt?esc(dateText(endsAt)):'不限时，逐页采集到末页'}<br>提醒：${esc(payload.notify_email || '不发送邮件')}<br>当前 IP 白名单：${payload.whitelist.length} 条${payload.include_company?'，并入公司共享白名单':''}</p><div class="actions"><button type="button" class="btn" id="edit-task">返回编辑</button><button type="button" class="btn primary" id="confirm-run">确认并立即运行</button></div>`;
  $('#edit-task').onclick = () => { confirmation.hidden = true; $('#task-fields').hidden = false; $('.task-submit').hidden = false; $('#submit-task').focus(); };
  $('#confirm-run').onclick = () => executeTask(payload);
  $('#confirm-run').focus();
}

async function executeTask(payload) {
  if (state.taskSubmitting) return;
  const epoch = state.viewEpoch, fields = $('#task-fields');
  state.taskSubmitting = true;
  fields.disabled = true;
  saveTaskDraft();
  try { await withBusy($('#confirm-run'), '提交中…', async () => {
    try {
    const data = await api('/api/tasks', { method: 'POST', body: JSON.stringify(payload) });
    state.lastTask = data.task.task_no;
    if (epoch !== state.viewEpoch || state.view !== 'tasks') return;
    closeModal(true);
    toast(payload.ends_at ? '任务已启动；按设定时间停止常规采集，补采收尾后结束任务' : '任务已启动；不限量、不限时，逐页采集到末页并完成补采', true);
    await refreshTasks();
    } catch (err) { toast(err.message); }
  }); } finally {
    state.taskSubmitting = false; fields.disabled = false;
    if (state.view === 'tasks' && $('#task-fields')) { $('#task-fields').disabled = false; $('#submit-task').disabled = false; updateTaskSummary(); }
  }
}

function renderTaskTable() {
  const body = $('#task-body');
  if (!body) return;
  const running = state.tasks.filter(t => ['running','queued'].includes(t.status)).length;
  $('#task-metrics').innerHTML = [['运行中',running],['已完成',state.tasks.filter(t=>t.status==='success').length],['需关注',state.tasks.filter(t=>t.status==='failed'||t.mail_status==='failed').length]].map(([label,n])=>`<div class="metric"><span>${label}</span><strong>${n}</strong></div>`).join('');
  if (!state.tasks.length) { body.innerHTML = '<tr><td colspan="6" class="empty">还没有任务，点击“新建任务”开始采集。</td></tr>'; return; }
  body.innerHTML = state.tasks.map((task) => {
    const result = task.result || {};
    const statusClass = task.status === 'success' ? 'ok' : (task.status === 'failed' ? 'bad' : '');
    return `<tr>
      <td><strong>${esc(task.ip_name)}</strong><div class="muted small">${esc(task.task_no)} · ${task.mode === 'dry_run' ? '采集演练' : '正式采集'}</div></td>
      <td class="muted small">${esc(task.platform_labels.join('、'))}</td>
      <td><span class="pill ${statusClass}">${esc(task.status_label)}</span>${task.mail_status==='failed'?'<div class="small muted">邮件发送失败</div>':''}</td>
      <td>${result.candidates != null ? result.candidates : '—'}</td>
      <td class="muted small">${esc(task.created_at)}</td>
       <td><div class="row"><button class="btn small" data-task="${esc(task.task_no)}">详情</button>${['queued','scheduled'].includes(task.status) && (isAdmin() || task.created_by===state.me.id) ? `<button class="btn small" data-cancel="${esc(task.task_no)}">取消</button>` : ''}</div></td>
    </tr>`;
  }).join('');
  $$('#task-body button[data-cancel]').forEach((btn) => btn.onclick = async () => {
    const no = btn.dataset.cancel;
    showModal(`<h3>取消未开始的任务？</h3><p class="help">${esc(no)}</p><p>取消后，本次任务将不再执行。已生成的线索不受影响。</p><div class="actions"><button class="btn" id="m-cancel">保留任务</button><button class="btn danger" id="m-apply">确认取消</button></div>`, root => {
      root.querySelector('#m-cancel').onclick = closeModal;
      root.querySelector('#m-apply').onclick = () => withBusy(root.querySelector('#m-apply'), '取消中…', async () => {
        try { await api('/api/tasks/' + encodeURIComponent(no) + '/cancel', {method:'POST'}); closeModal(true); toast('任务已取消', true); await refreshTasks(); } catch (err) { toast(err.message); }
      });
    });
  });
  $$('#task-body button[data-task]').forEach(btn => btn.onclick = () => openTask(btn.dataset.task));
  labelTables();
}

function openTask(no) {
  const task = state.tasks.find(t => t.task_no === no);
  if (!task) return;
  const result = task.result || {};
  showModal(`<h3>任务详情</h3><div class="code-box">${esc(no)}</div><dl class="summary-list"><dt>IP / 状态</dt><dd>${esc(task.ip_name)} · ${esc(task.status_label)}</dd><dt>模式 / 平台</dt><dd>${task.mode==='dry_run'?'采集演练':'正式运行'} · ${esc((task.platform_labels||[]).join('、'))}</dd><dt>关键词</dt><dd>${esc((task.keywords||[]).join('、'))}</dd><dt>创建 / 完成</dt><dd>${esc(task.created_at)} / ${esc(task.ended_at || '尚未完成')}</dd><dt>设定常规采集截止时间</dt><dd>${esc(task.ends_at || '不限时，逐页采集到末页')}</dd><dt>常规采集停止原因</dt><dd>${result.stop_reason==='deadline'?'到达常规采集截止时间；整体完成时间见上方，补采失败项见下方异常摘要':'—'}</dd><dt>提醒邮箱</dt><dd>${esc(task.notify_email || '未填写')}</dd><dt>候选线索</dt><dd>${esc(result.candidates ?? '尚无结果')}</dd><dt>Excel</dt><dd>${task.excel_path ? `<a class="btn small" href="/api/tasks/${encodeURIComponent(no)}/export">下载本次 Excel ↗</a><div class="help">${esc(task.excel_path)}</div>` : '尚未生成'}</dd></dl>
    <dl class="summary-list"><dt>实际开始</dt><dd>${esc(task.started_at || '尚未开始')}</dd><dt>风险分布</dt><dd>达标 ${esc(result.risks?.达标||0)} · 待核验 ${esc(result.risks?.待核验||0)} · 临界 ${esc(result.risks?.临界||0)}</dd><dt>邮件状态</dt><dd>${esc(({sent:'已发送',failed:'发送失败',skipped:'未发送',pending:'等待发送'})[task.mail_status] || task.mail_status || '暂无记录')}</dd></dl>
    <div class="task-platform-results">${Object.entries(result.platforms || {}).map(([name, p]) => `<div class="notice"><strong>${esc(p.label || name)}</strong> · 采集 ${esc(p.items || 0)} 条 / 候选 ${esc(p.leads || 0)} 条<p>${p.amount_missing != null ? `金额${p.amount_missing > 0 ? '数据不完整' : '数据完整'}：可计算 ${esc(p.amount_complete ?? 0)} 条 / 待核验 ${esc(p.amount_missing)} 条` : '金额完整性：未统计（历史任务）'}</p><p>金额采用销量×单价；没有有效销量时，按评论数直接×单价估算。</p>${p.missing_fields?.shop_name != null ? `<p>店名待补齐 ${esc(p.missing_fields.shop_name)} 条</p>` : '<p>店名完整性：未统计（历史任务）</p>'}${p.missing_fields?.title != null ? `<p>标题待补齐 ${esc(p.missing_fields.title)} 条 · 真实销量待补齐 ${esc(p.missing_fields.sales ?? 0)} 条</p>` : '<p>标题完整性：未统计（历史任务）</p>'}${p.amount_missing > 0 ? `<p>缺价格 ${esc(p.missing_fields?.price ?? 0)} 条 · 缺销量 ${esc(p.missing_fields?.sales ?? 0)} 条 · 缺评论 ${esc(p.missing_fields?.comments ?? 0)} 条；无法计算的金额保留为空，未按 0 计算。</p>` : ''}<p>${esc(p.error || p.status || '无状态')}</p></div>`).join('')}</div>
    ${task.error || result.errors?.length ? `<div class="inline-error">${esc(task.error || result.errors.join('；'))}</div>` : ''}
    ${task.mail_error ? `<p class="help">邮件：${esc(task.mail_error)}</p>` : ''}
    <div class="actions"><button class="btn primary" id="m-cancel">关闭详情</button></div>`, root => { root.querySelector('#m-cancel').onclick = closeModal; });
}

async function loadScheduled() {
  const epoch = state.viewEpoch;
  try {
    const data = await api('/api/scheduled');
    if (epoch !== state.viewEpoch || state.view !== 'tasks') return;
    const el = $('#scheduled-list');
    if (!el) return;
    const rows = (data.items || []).filter(row => row.status === 'scheduled');
    $('#legacy-scheduled').hidden = rows.length === 0;
    el.innerHTML = rows.length ? rows.map((row) => `<div class="row" style="justify-content:space-between;border-bottom:1px dashed var(--line);padding:6px 0"><span>${esc(row.run_at)} · ${esc(row.ip_name)} · ${esc(row.status)}</span></div>`).join('') : '没有预约任务';
  } catch (err) { if (epoch === state.viewEpoch && $('#scheduled-list')) $('#scheduled-list').textContent = '预约记录加载失败，请刷新记录重试。'; }
}

function schedulePoll(delay = 4000) {
  clearTimeout(state.pollTimer);
  if (state.view !== 'tasks') return;
  state.pollTimer = setTimeout(refreshTasks, delay);
}

/* ------------------------------------------------------------------ */
/* IP / 白名单 */
async function renderIps() {
  const epoch = state.viewEpoch;
  const data = await api('/api/ips');
  if (epoch !== state.viewEpoch || state.view !== 'ips') return;
  state.ips = data.items;
  $('#main').innerHTML = `<div class="page-head"><div><h2>IP / 白名单</h2><div class="desc">集中管理监测档案与授权店铺，减少重复复核。</div></div><div class="row"><button class="btn primary" id="add-ip">新增 IP 档案</button></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>IP</th><th>公司</th><th>关键词</th><th>白名单</th><th>状态</th><th></th></tr></thead><tbody id="ip-body"></tbody></table></div></div>`;
  $('#add-ip').onclick = () => showModal('<h3>新增 IP 档案</h3><form id="new-ip-form"><label class="field">IP 名称<input id="new-ip-name" required maxlength="200" autofocus></label><p class="help">建立档案后，可添加关键词、公司信息与授权店铺。</p><div class="actions"><button type="button" class="btn" id="m-cancel">取消</button><button type="submit" class="btn primary" id="m-save">创建档案</button></div></form>', root => {
    root.querySelector('#m-cancel').onclick = closeModal;
    root.querySelector('#new-ip-form').onsubmit = e => { e.preventDefault(); withBusy(root.querySelector('#m-save'), '创建中…', async () => {
      const name = root.querySelector('#new-ip-name').value.trim();
      if (!name) { toast('请填写 IP 名称'); return; }
      try { await api('/api/ips', {method:'POST',body:JSON.stringify({name})}); closeModal(true); toast('档案已创建',true); if (epoch===state.viewEpoch) await renderIps(); } catch (err) { toast(err.message); }
    }); };
  });
  const body = $('#ip-body');
  body.innerHTML = state.ips.length ? state.ips.map((ip) => `<tr>
    <td><strong>${esc(ip.name)}</strong><div class="muted small">${esc(ip.name_norm)}</div></td>
    <td>${esc(ip.company_code || '—')}<div class="muted small">${esc(ip.company_name || '')}</div></td>
    <td class="muted small">${esc((ip.keywords || []).join('、') || '—')}</td>
    <td>${ip.whitelist_count}</td>
    <td>${ip.active ? '<span class="pill ok">启用</span>' : '<span class="pill">停用</span>'}</td>
    <td><button class="btn small" data-edit="${ip.id}">管理档案</button></td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">还没有 IP 档案</td></tr>';
  $$('#ip-body button[data-edit]').forEach((btn) => btn.onclick = () => openIpEditor(Number(btn.dataset.edit)));
  labelTables();
}

function openIpEditor(id) {
  const ip = state.ips.find((item) => item.id === id);
  if (!ip) return;
  showModal(`<h3>编辑 IP 档案</h3>
    <label class="field">公司代码<input id="m-company"></label><div style="height:8px"></div>
    <label class="field">公司名称<input id="m-company-name"></label><div style="height:8px"></div>
    <label class="field">关键词（逗号分隔）<input id="m-keywords"></label><div style="height:8px"></div>
    <label class="field">参考图（每行一个路径/URL）<textarea id="m-images" rows="3"></textarea></label>
    <div class="row" style="margin-top:20px"><button class="btn small" id="manage-company">公司共享白名单</button><button class="btn small ${ip.active?'danger':'ok'}" id="toggle-ip">${ip.active?'停用':'启用'}档案</button></div><div class="actions"><button class="btn" id="m-cancel">取消</button><button class="btn primary" id="m-save">保存档案</button></div>`, (root) => {
    root.querySelector('#m-company').value = ip.company_code || '';
    root.querySelector('#m-company-name').value = ip.company_name || '';
    root.querySelector('#m-keywords').value = (ip.keywords || []).join(',');
    root.querySelector('#m-images').value = (ip.ref_images || []).join('\n');
    root.querySelector('#m-cancel').onclick = closeModal;
    root.querySelector('#manage-company').onclick = () => openCompanyWhitelist(id);
    root.querySelector('#toggle-ip').onclick = () => {
      showModal(`<h3>${ip.active?'停用':'启用'}档案？</h3><p>${esc(ip.name)}</p><div class="actions"><button class="btn" id="m-cancel">返回</button><button class="btn primary" id="m-apply">确认${ip.active?'停用':'启用'}</button></div>`, target => {
        target.querySelector('#m-cancel').onclick = () => openIpEditor(id);
        target.querySelector('#m-apply').onclick = () => withBusy(target.querySelector('#m-apply'),'保存中…',async () => {
          try { await api('/api/ips/'+id,{method:'PUT',body:JSON.stringify({active:!ip.active})}); closeModal(true); toast('档案已更新',true); if(state.view==='ips') await renderIps(); } catch(err) { toast(err.message); }
        });
      });
    };
    root.querySelector('#m-save').onclick = () => withBusy(root.querySelector('#m-save'), '保存中…', async () => {
      try {
        await api('/api/ips/' + ip.id, { method: 'PUT', body: JSON.stringify({
          company_code: root.querySelector('#m-company').value.trim(),
          company_name: root.querySelector('#m-company-name').value.trim(),
          keywords: root.querySelector('#m-keywords').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          ref_images: root.querySelector('#m-images').value.split('\n').map((s) => s.trim()).filter(Boolean),
        }) });
        closeModal(true); toast('已保存', true); if (state.view==='ips') await renderIps();
      } catch (err) { toast(err.message); }
    });
  });
}

function openCompanyWhitelist(id) {
  const ip = state.ips.find((item) => item.id === id);
  if (!ip) return;
  showModal(`<h3>公司共享白名单</h3><p class="help">公司：${esc(ip.company_code || '（未填写公司代码）')}。每行「平台,店铺名[,店铺ID]」。</p>
    <label class="field">条目<textarea id="m-lines" rows="6" placeholder="taobao,某某旗舰店"></textarea></label>
    <div class="actions"><button class="btn" id="m-cancel">取消</button><button class="btn primary" id="m-save">添加</button></div>`, (root) => {
    root.querySelector('#m-cancel').onclick = closeModal;
    root.querySelector('#m-save').onclick = () => withBusy(root.querySelector('#m-save'), '添加中…', async () => {
      if (!ip.company_code) { toast('请先给 IP 填写公司代码'); return; }
      const entries = root.querySelector('#m-lines').value.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
        const parts = line.split(',');
        return { platform_group: parts[0], shop_name: parts[1] || '', shop_id: parts[2] || '' };
      }).filter((item) => item.shop_name);
      if (!entries.length) { toast('请至少填写一条有效的店铺信息'); return; }
      try {
        await api('/api/ips/' + ip.id + '/company-whitelist', { method: 'POST', body: JSON.stringify({ company_code: ip.company_code, entries }) });
        closeModal(true); toast('已添加', true); if(state.view==='ips') await renderIps();
      } catch (err) { toast(err.message); }
    });
  });
}

/* ------------------------------------------------------------------ */
/* 账号 / 邀请 */
async function renderAccounts() {
  const epoch = state.viewEpoch;
  const [users, invites] = await Promise.all([api('/api/users'), api('/api/invites')]);
  if (epoch !== state.viewEpoch || state.view !== 'accounts') return;
  $('#main').innerHTML = `<div class="page-head"><div><h2>账号 / 邀请</h2><div class="desc">管理团队成员，通过邀请授予工作台访问权限。</div></div><button class="btn primary" id="open-invite">生成邀请</button></div>
    <div class="subnav" aria-label="成员管理视图"><button data-team-tab="users">团队账号</button><button data-team-tab="invites">邀请记录</button></div>
    <template id="invite-template"><h3>生成邀请</h3><form id="invite-form"><div class="grid3">
      <label class="field">角色<select id="i-role"><option value="user">普通用户</option>${isSupervisor()?'<option value="admin">管理员</option>':''}</select></label>
      <label class="field">有效期（分钟）<input id="i-expires" type="number" value="1440" min="5" required></label>
      <label class="field">可用次数<input id="i-uses" type="number" value="1" min="1" required></label>
    </div><div class="actions"><button class="btn" type="button" id="m-cancel">取消</button><button class="btn primary" type="submit" id="create-invite">生成邀请码</button></div></form></template>
    <div class="card" data-team-panel="invites"><h3>邀请记录</h3><div class="table-wrap"><table><thead><tr><th>角色</th><th>已用/次数</th><th>到期</th><th>状态</th><th>操作</th></tr></thead><tbody id="invite-body"></tbody></table></div></div>
    <div class="card" data-team-panel="users"><h3>团队账号</h3><div class="table-wrap"><table><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead><tbody id="user-body"></tbody></table></div></div>`;
  const setTab = key => {
    state.accountsTab = key;
    $$('[data-team-panel]').forEach(el => { el.hidden = el.dataset.teamPanel !== key; });
    $$('[data-team-tab]').forEach(el => { el.classList.toggle('active', el.dataset.teamTab === key); el.setAttribute('aria-pressed',String(el.dataset.teamTab===key)); });
  };
  $$('[data-team-tab]').forEach(btn => btn.onclick = () => setTab(btn.dataset.teamTab));
  setTab(state.accountsTab);
  $('#open-invite').onclick = () => {
    showModal($('#invite-template').innerHTML, root => {
      root.querySelector('#m-cancel').onclick = closeModal;
      root.querySelector('#invite-form').onsubmit = e => { e.preventDefault(); withBusy(root.querySelector('#create-invite'), '生成中…', async () => {
    try {
      const data = await api('/api/invites', { method: 'POST', body: JSON.stringify({ role: $('#i-role').value, expires_minutes: Number($('#i-expires').value), max_uses: Number($('#i-uses').value) }) });
      closeModal(true);
      const registerUrl = location.origin + data.invite.register_url;
      showModal(`<h3>邀请已生成</h3><p class="help">邀请码只在本次生成后显示，请在关闭前复制保存。</p><label class="field">邀请码<input id="invite-result" value="${esc(data.invite.code)}" readonly></label><label class="field">注册链接<input id="register-link" value="${esc(registerUrl)}" readonly></label><div class="actions"><button class="btn" id="copy-invite">复制邀请码</button><button class="btn primary" id="m-cancel">完成</button></div>`, result => {
        result.querySelector('#copy-invite').onclick = async () => {
          try { await navigator.clipboard.writeText(data.invite.code); toast('邀请码已复制',true); }
          catch (err) { result.querySelector('#invite-result').select(); toast('请复制已选中的邀请码'); }
        };
        result.querySelector('#m-cancel').onclick = closeModal;
      });
      toast('邀请码已生成', true);
      if (epoch === state.viewEpoch) { state.accountsTab='invites'; await renderAccounts(); }
    } catch (err) { toast(err.message); }
      }); };
    });
  };
  const ib = $('#invite-body');
  ib.innerHTML = invites.items.length ? invites.items.map((inv) => `<tr><td>${esc(inv.role_label)}</td><td>${inv.used_count}/${inv.max_uses}</td><td>${esc(inv.expires_at)}</td><td>${inv.expired||!inv.active?'<span class="pill bad">失效</span>':'<span class="pill ok">有效</span>'}</td><td>${inv.active?`<button class="btn small" data-revoke="${inv.id}">作废</button>`:''}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">还没有邀请</td></tr>';
  $$('#invite-body button[data-revoke]').forEach((btn) => btn.onclick = async () => {
    try { await api('/api/invites/' + btn.dataset.revoke + '/revoke', { method: 'POST' }); toast('已作废', true); renderAccounts(); } catch (err) { toast(err.message); }
  });
  const ub = $('#user-body');
  ub.innerHTML = users.items.map((u) => `<tr><td><strong>${esc(u.username)}</strong></td><td>${esc(u.role_label)}</td><td>${u.status==='active'?'<span class="pill ok">启用</span>':'<span class="pill bad">停用</span>'}</td><td class="muted small">${esc(u.last_login_at||'—')}</td><td>
    ${u.status==='active'&&u.role!=='supervisor'?`<button class="btn small danger" data-disable="${u.id}">停用</button>`:''}
    ${isSupervisor()&&u.role!=='supervisor'?`<button class="btn small" data-role="${u.id}" data-current="${u.role}">${u.role==='admin'?'降为普通用户':'设为管理员'}</button>`:''}
    ${u.role!=='supervisor'?`<button class="btn small" data-reset="${u.id}">重置密码</button>`:''}
  </td></tr>`).join('');
  $$('#user-body button[data-disable]').forEach((btn) => btn.onclick = async () => {
    if (!confirm('停用后该账号立即退出且不能登录，确认？')) return;
    try { await api('/api/users/' + btn.dataset.disable + '/disable', { method: 'POST' }); toast('已停用', true); renderAccounts(); } catch (err) { toast(err.message); }
  });
  $$('#user-body button[data-role]').forEach((btn) => btn.onclick = async () => {
    const target = btn.dataset.current === 'admin' ? 'user' : 'admin';
    if (!confirm(`确认把该账号${target==='admin'?'设为管理员':'降为普通用户'}？`)) return;
    try { await api('/api/users/' + btn.dataset.role + '/role', { method: 'POST', body: JSON.stringify({ role: target }) }); toast('已更新', true); renderAccounts(); } catch (err) { toast(err.message); }
  });
  $$('#user-body button[data-reset]').forEach((btn) => btn.onclick = async () => {
    try { const data = await api('/api/users/' + btn.dataset.reset + '/reset-password', { method: 'POST' }); alert('临时密码（只显示一次）：' + data.temporary_password); } catch (err) { toast(err.message); }
  });
  labelTables();
}

/* ------------------------------------------------------------------ */
/* 设置 */
async function renderSettings() {
  const epoch = state.viewEpoch;
  const data = await api('/api/settings');
  if (epoch !== state.viewEpoch || state.view !== 'settings') return;
  const s = data.settings || {};
  const smtp = data.smtp_status || {};
  $('#main').innerHTML = `<div class="page-head"><div><h2>系统设置</h2><div class="desc">按用途管理通知、访问安全与采集默认值。</div></div></div>
    <div class="settings-layout"><nav class="settings-nav" aria-label="设置分区">${[['mail','邮件通知'],['security','安全与访问'],['tasks','任务与导出']].map(([key,label])=>`<button class="btn" data-settings-tab="${key}">${label}</button>`).join('')}</nav><div>
    <div class="card" data-settings-panel="mail"><h3>邮件通知</h3><div class="row" style="margin-bottom:18px"><span class="pill ${smtp.configured?'ok':'bad'}" id="smtp-status">${smtp.configured?'SMTP 已配置':'SMTP 未配置'}</span><span class="pill">授权码：${smtp.credential?'已配置':'未找到'}</span><span class="muted small">授权码保存在本机凭据管理器；未配置时仅记录通知日志。</span></div>
      <div class="grid2">
        <label class="field">SMTP 主机<input id="s-smtp-host"></label>
        <label class="field">端口<input id="s-smtp-port" type="number"></label>
        <label class="field">发件邮箱<input id="s-smtp-from"></label>
        <label class="field">默认收件人（逗号分隔）<input id="s-smtp-to"></label>
        <label class="field">keyring 服务名<input id="s-smtp-keyring"></label>
        <label class="field">SSL<select id="s-smtp-ssl"><option value="1">是</option><option value="0">否</option></select></label>
      </div></div>
    <div class="card" data-settings-panel="security"><h3>安全与访问</h3><div class="grid2">
      <label class="field">允许的内网网段（逗号分隔）<input id="s-networks"></label>
      <label class="field">会话空闲小时<input id="s-idle" type="number" min="1"></label>
      <label class="field">登录失败锁定阈值<input id="s-threshold" type="number" min="1"></label>
      <label class="field">锁定时长（分钟）<input id="s-lock" type="number" min="1"></label>
    </div></div><div class="card" data-settings-panel="tasks"><h3>任务与导出</h3><div class="grid2">
      <label class="field">默认提醒邮箱<input id="s-email"></label>
      <label class="field">默认并入公司白名单<select id="s-company"><option value="1">是</option><option value="0">否</option></select></label>
      <label class="field">导出目录<input id="s-export"></label>
    </div></div><div class="row"><button class="btn primary" id="save-settings">保存当前分区</button><span class="help" id="settings-hint" role="status">切换分区会保留当前未保存的输入。</span></div></div></div>`;
  const set = (id, value) => { const el = $(id); if (el) el.value = value == null ? '' : value; };
  set('#s-smtp-host', s.smtp_host); set('#s-smtp-port', s.smtp_port); set('#s-smtp-from', s.smtp_from); set('#s-smtp-to', s.smtp_to);
  set('#s-smtp-keyring', s.smtp_keyring_service); set('#s-smtp-ssl', s.smtp_ssl); set('#s-networks', s.allowed_networks);
  set('#s-idle', s.session_idle_hours); set('#s-threshold', s.login_lock_threshold); set('#s-lock', s.login_lock_minutes);
  set('#s-email', s.notify_email_default); set('#s-company', s.company_whitelist_default); set('#s-export', s.export_dir);
  const groups = {
    mail: ['smtp_host','smtp_port','smtp_from','smtp_to','smtp_keyring_service','smtp_ssl'],
    security: ['allowed_networks','session_idle_hours','login_lock_threshold','login_lock_minutes'],
    tasks: ['notify_email_default','company_whitelist_default','export_dir'],
  };
  const switchSection = key => {
    state.settingsTab = key;
    $$('[data-settings-panel]').forEach(el => { el.hidden = el.dataset.settingsPanel !== key; });
    $$('[data-settings-tab]').forEach(el => { el.classList.toggle('primary',el.dataset.settingsTab===key); el.setAttribute('aria-pressed',String(el.dataset.settingsTab===key)); });
  };
  $$('[data-settings-tab]').forEach(btn => btn.onclick = () => switchSection(btn.dataset.settingsTab));
  switchSection(state.settingsTab);
  $('#save-settings').onclick = () => withBusy($('#save-settings'), '保存中…', async () => {
    const section = state.settingsTab;
    if ([...$(`[data-settings-panel="${section}"]`).querySelectorAll('input')].some(el=>!el.reportValidity())) return;
    try {
      const values = {
        smtp_host: $('#s-smtp-host').value.trim(), smtp_port: $('#s-smtp-port').value, smtp_from: $('#s-smtp-from').value.trim(),
        smtp_to: $('#s-smtp-to').value.trim(), smtp_keyring_service: $('#s-smtp-keyring').value.trim(), smtp_ssl: $('#s-smtp-ssl').value,
        allowed_networks: $('#s-networks').value.trim(), session_idle_hours: $('#s-idle').value,
        login_lock_threshold: $('#s-threshold').value, login_lock_minutes: $('#s-lock').value,
        notify_email_default: $('#s-email').value.trim(), company_whitelist_default: $('#s-company').value, export_dir: $('#s-export').value.trim(),
      };
      const payload = Object.assign({}, s, Object.fromEntries(groups[section].map(key=>[key,values[key]])));
      const result = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
      Object.assign(s, result.settings || payload);
      if (epoch !== state.viewEpoch) return;
      $('#settings-hint').textContent = '当前分区已保存，其他分区未保存的输入仍保留。';
      $('#smtp-status').textContent = result.smtp_status?.configured ? 'SMTP 已配置' : 'SMTP 未配置';
      $('#smtp-status').className = 'pill ' + (result.smtp_status?.configured ? 'ok' : 'bad');
      toast('当前分区设置已保存', true);
    } catch (err) { toast(err.message); }
  });
}

/* ------------------------------------------------------------------ */
/* 版本更新 */
async function renderUpdate() {
  const epoch = state.viewEpoch;
  const data = await api('/api/admin/versions');
  if (epoch !== state.viewEpoch || state.view !== 'update') return;
  const current = data.current || {};
  $('#main').innerHTML = `<div class="page-head"><div><h2>版本更新</h2><div class="desc">上传 zip 版本包；更新前自动备份当前版本，可一键回滚到上一版。数据目录不随版本切换。</div></div></div>
    <div class="card"><h3>当前版本</h3><div class="kv"><span class="k">版本号</span><span><strong>${esc(current.version||'—')}</strong></span><span class="k">安装时间</span><span>${esc(current.installed_at||'—')}</span></div><details><summary>安装位置</summary><div class="code-box">${esc(current.path||'—')}</div></details></div>
    <div class="card"><h3>上传版本包</h3><label class="field">zip 文件<input id="u-file" type="file" accept=".zip"></label><div style="height:10px"></div><label class="field">更新说明<input id="u-note" placeholder="版本说明（选填）"></label><div style="height:12px"></div><button class="btn primary" id="u-upload">上传并安装</button><div class="help" id="u-hint" style="margin-top:8px">安装成功后重启工作台服务生效；安装失败会自动保留旧版本。</div></div>
    <div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">版本历史</h3><button class="btn danger small" id="u-rollback">回滚到上一版</button></div><div class="table-wrap"><table><thead><tr><th>版本</th><th>状态</th><th>时间</th><th>说明</th></tr></thead><tbody id="v-body"></tbody></table></div></div>`;
  const body = $('#v-body');
  body.innerHTML = (data.releases||[]).length ? data.releases.map((v) => `<tr><td>${esc(v.version)}</td><td>${esc(v.status)}</td><td class="muted small">${esc(v.created_at)}</td><td class="muted small">${esc(v.note||'')}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">还没有版本记录</td></tr>';
  $('#u-upload').onclick = () => {
    const file = $('#u-file').files[0];
    if (!file) { toast('请选择 zip 文件'); return; }
    if (!file.name.toLowerCase().endsWith('.zip')) { toast('只接受 zip 文件'); return; }
    if (file.size > 64 * 1024 * 1024) { toast('版本包不能超过 64 MB'); return; }
    const note = $('#u-note').value.trim();
    showModal(`<h3>确认安装版本包？</h3><p>${esc(file.name)}</p><p class="help">安装前会自动备份当前版本，安装成功后需要重启服务。</p><div class="actions"><button class="btn" id="m-cancel">返回检查</button><button class="btn primary" id="m-apply">确认安装</button></div>`, root => {
      root.querySelector('#m-cancel').onclick = closeModal;
      root.querySelector('#m-apply').onclick = () => withBusy(root.querySelector('#m-apply'), '安装中…', async () => {
        try {
          const bytes = new Uint8Array(await file.arrayBuffer()); let binary = '';
          for (let i=0;i<bytes.length;i+=0x8000) binary += String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));
          await api('/api/admin/update',{method:'POST',body:JSON.stringify({filename:file.name,content_base64:btoa(binary),note})});
          closeModal(true); toast('版本已安装，请重启服务生效',true); if(epoch===state.viewEpoch) await renderUpdate();
        } catch(err) { toast(err.message); }
      });
    });
  };
  $('#u-rollback').onclick = () => showModal('<h3>回滚到上一版本？</h3><p>当前版本会先自动备份，回滚后需要重启服务生效。</p><div class="actions"><button class="btn" id="m-cancel">保留当前版本</button><button class="btn danger" id="m-apply">确认回滚</button></div>',root=>{
    root.querySelector('#m-cancel').onclick=closeModal;
    root.querySelector('#m-apply').onclick=()=>withBusy(root.querySelector('#m-apply'),'回滚中…',async()=>{
      try { await api('/api/admin/rollback',{method:'POST'}); closeModal(true); toast('已回滚，请重启服务生效',true); if(epoch===state.viewEpoch) await renderUpdate(); } catch(err) { toast(err.message); }
    });
  });
}

/* ------------------------------------------------------------------ */
/* 日志 */
async function renderLogs() {
  const epoch = state.viewEpoch;
  let request = 0;
  $('#main').innerHTML = `<div class="page-head"><div><h2>审计 / 邮件</h2><div class="desc">审计日志只增不改；邮件日志记录每封完成通知结果。</div></div></div>
    <div class="card"><div class="subnav" aria-label="日志类型"><button id="tab-audit">审计日志</button><button id="tab-mail">邮件日志</button></div><div id="log-body" class="table-wrap"></div></div>`;
  const show = async (kind) => {
    const current = ++request;
    ['audit','mail'].forEach(key => { $('#tab-'+key).classList.toggle('active',key===kind); $('#tab-'+key).setAttribute('aria-pressed',String(key===kind)); });
    $('#log-body').innerHTML = '<div class="empty" role="status">正在加载日志…</div>';
    try {
    const data = kind === 'audit' ? await api('/api/audit?limit=300') : await api('/api/mail-logs');
    if (epoch !== state.viewEpoch || current !== request || state.view !== 'logs') return;
    const el = $('#log-body');
    if (kind === 'audit') {
      el.innerHTML = `<table><thead><tr><th>时间</th><th>账号</th><th>来源 IP</th><th>操作</th><th>对象</th></tr></thead><tbody>${(data.items||[]).map((row) => `<tr><td class="muted small">${esc(row.created_at)}</td><td>${esc(row.actor_name)}</td><td class="muted small">${esc(row.source_ip)}</td><td>${esc(row.action)}</td><td class="muted small">${esc(row.object_type)} ${esc(row.object_id)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无日志</td></tr>'}</tbody></table>`;
    } else {
      el.innerHTML = `<table><thead><tr><th>时间</th><th>任务</th><th>收件人</th><th>主题</th><th>结果</th><th>原因</th></tr></thead><tbody>${(data.items||[]).map((row) => `<tr><td class="muted small">${esc(row.sent_at)}</td><td>${esc(row.task_no)}</td><td class="muted small">${esc(row.recipients)}</td><td class="muted small">${esc(row.subject)}</td><td>${esc(row.status)}</td><td class="muted small">${esc(row.error||'')}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无邮件日志</td></tr>'}</tbody></table>`;
    }
    labelTables();
    } catch(err) { if(epoch===state.viewEpoch && current===request) { $('#log-body').innerHTML='<div class="empty">加载失败，请重新选择日志类型重试。</div>'; toast(err.message); } }
  };
  $('#tab-audit').onclick = () => show('audit');
  $('#tab-mail').onclick = () => show('mail');
  return show('audit');
}

/* ------------------------------------------------------------------ */
/* 修改密码 */
async function renderAccount() {
  $('#main').innerHTML = `<div class="page-head"><div><h2>修改密码</h2><div class="desc">当前账号：${esc(state.me.username)}（${esc(state.me.role_label)}）</div></div></div>
    <div class="card" style="max-width:520px"><label class="field">原密码<input id="p-old" type="password"></label><div style="height:10px"></div>
    <label class="field">新密码（至少 8 位，含字母和数字）<input id="p-new" type="password"></label><div style="height:10px"></div>
    <label class="field">确认新密码<input id="p-confirm" type="password"></label><div style="height:14px"></div>
    <button class="btn primary" id="save-password">保存</button></div>`;
  $('#save-password').onclick = async () => {
    if ($('#p-new').value !== $('#p-confirm').value) { toast('两次密码不一致'); return; }
    try { await api('/api/account/password', { method: 'POST', body: JSON.stringify({ old_password: $('#p-old').value, new_password: $('#p-new').value, confirm: $('#p-confirm').value }) }); toast('密码已更新', true); } catch (err) { toast(err.message); }
  };
}

/* ------------------------------------------------------------------ */
function showModal(html, setup) {
  closeModal(true);
  const root = $('#modal-root');
  root.innerHTML = `<dialog id="modal-overlay" class="modal-dialog" aria-label="操作对话框"><div class="modal">${html}</div></dialog>`;
  const dialog = $('#modal-overlay');
  const title = dialog.querySelector('h3');
  if (title) { title.id = 'modal-title'; dialog.setAttribute('aria-labelledby','modal-title'); dialog.removeAttribute('aria-label'); }
  dialog.onclick = (e) => { if (e.target === dialog) closeModal(); };
  dialog.oncancel = e => { e.preventDefault(); closeModal(); };
  if (setup) setup(root);
  dialog.showModal();
}
function closeModal(force = false) {
  const dialog = $('#modal-root dialog');
  if (force !== true && dialog?.querySelector('[aria-busy="true"]')) return;
  if (dialog?.querySelector('#task-form')) { saveTaskDraft(); state.previewRequest++; }
  if (dialog) dialog.close();
  $('#modal-root').innerHTML = '';
}

window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1);
  if (view !== state.view && navItems().some(i => i.key === view)) navigate(view);
});

(async function init() {
  try {
    $('#menu-toggle').onclick = () => setMenu(!$('.sidebar').classList.contains('open'));
    $('#nav-scrim').onclick = () => { setMenu(false); $('#menu-toggle').focus(); };
    document.addEventListener('keydown', e => {
      if (!$('.sidebar').classList.contains('open') || !matchMedia('(max-width:680px)').matches) return;
      if (e.key === 'Escape') { setMenu(false); $('#menu-toggle').focus(); }
      if (e.key === 'Tab') {
        const buttons = [...$('.sidebar').querySelectorAll('button:not(:disabled)')];
        if (e.shiftKey && document.activeElement === buttons[0]) { e.preventDefault(); buttons.at(-1).focus(); }
        else if (!e.shiftKey && document.activeElement === buttons.at(-1)) { e.preventDefault(); buttons[0].focus(); }
      }
    });
    matchMedia('(max-width:680px)').addEventListener('change', () => setMenu(false));
    let solid = false;
    try { solid = localStorage.getItem('workbench-material') === 'solid'; } catch(err) {}
    const applyMaterial = value => { document.body.dataset.material = value?'solid':'glass'; $('#material-toggle').setAttribute('aria-pressed',String(value)); $('#material-toggle').textContent=value?'恢复玻璃效果':'降低透明度'; };
    applyMaterial(solid);
    $('#material-toggle').onclick = () => { solid=!solid; applyMaterial(solid); try { localStorage.setItem('workbench-material',solid?'solid':'glass'); } catch(err) {} };
    const me = await api('/api/me');
    state.me = me.user; state.csrf = me.csrf_token; state.version = me.version || '';
    if (me.bootstrap_required) { location.href = '/setup'; return; }
    renderShell();
    const view = (location.hash || '#leads').replace('#', '') || 'leads';
    navigate(navItems().some((item) => item.key === view) ? view : 'leads');
  } catch (err) {
    if (err.message !== '未登录') { location.href = '/login'; }
  }
})();
