'use strict';

const byId = id => document.getElementById(id);
const pending = new Map();
const number = new Intl.NumberFormat('zh-CN');
const dollars = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
const compact = new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1});
const dateTime = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
const outcomes = {success: '成功', failed: '失败', limited: '受限', canceled: '已取消'};
const viewButtons = [...document.querySelectorAll('[data-view]')];
let snapshot;
let editingKey;
let keyRemoval;
let usageKey;
let keyHistory;
let activeView = 'keys';
let analyticsPeriod = '1h';
let loadedAnalyticsPeriod;
let analyticsData;
let analyticsRefreshedAt = 0;
let refreshTask;
let refreshTimer;
const refreshIntervalMs = 2000;
const retryIntervalMs = 8000;
let operationPending = false;
let usageEpoch = 0;
let keyRowsSignature;
let keyPage = 0;
let settings;
let settingsEpoch = 0;
const keysPerPage = 15;

window.safeCodeNativeResponse = ({id, result, error}) => {
  const request = pending.get(id);
  if (!request) return;
  pending.delete(id);
  if (error) request.reject(new Error(error));
  else request.resolve(result);
};
function rpc(op, args = {}) {
  if (!window.chrome?.webview) return Promise.reject(new Error('请从 Safe Code 客户端打开网关管理。'));
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    pending.set(id, {resolve, reject});
    try {
      window.chrome.webview.postMessage({id, op, args});
    } catch (error) {
      pending.delete(id);
      reject(error);
    }
  });
}
const manage = (action, options = {}) => rpc('safeCodeApi.manage', {action, ...options});
const fmt = value => value == null ? '—' : number.format(value);
const slotLabel = item => fmt(item?.concurrency_text ? BigInt(item.concurrency_text) : item?.concurrency ?? 1);
const slotProgressMaximum = item => Math.min(item?.concurrency ?? 1, Number.MAX_VALUE);
const usd = nanos => nanos == null ? '—' : nanos > 0 && nanos < 100000 ? '< $0.0001' : dollars.format(nanos / 1e9);
const costLabel = item => usd(item.cost_nanos);
const timestamp = seconds => seconds ? dateTime.format(new Date(seconds * 1000)) : '—';
const duration = ms => ms == null ? '—' : ms >= 1000 ? (ms / 1000).toFixed(2) + ' 秒' : Math.round(ms) + ' 毫秒';
const shortId = id => id.slice(0, 12);
const keyName = key => key.note || shortId(key.id);
const activeCount = item => item?.active_count ?? Number(Boolean(item?.active));
const modelTokens = model => model.input + model.output + model.cache_read + model.cache_write;

function notice(id, text, error = false) {
  const node = byId(id);
  node.hidden = !text;
  node.textContent = text;
  node.dataset.error = String(error);
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function cell(row, value, className) {
  const td = element('td', null, className);
  if (value instanceof Node) td.append(value);
  else td.textContent = value;
  row.append(td);
  return td;
}
function pill(text, state) {
  const label = element('span', text, 'pill');
  label.dataset.state = state;
  return label;
}
function action(text, handler, className) {
  const button = element('button', text, className);
  button.type = 'button';
  button.onclick = handler;
  return button;
}

function renderOverview() {
  renderClaudeSubscription(byId('gateway-claude-limits'), snapshot.subscription);
  const {keys, waiting, stats} = snapshot;
  const current = keys.find(key => key.active);
  const running = activeCount(snapshot);
  const concurrencyButton = byId('gateway-concurrency');
  concurrencyButton.disabled = !snapshot.concurrency_text;
  concurrencyButton.textContent = '处理槽位 ' + slotLabel(snapshot);
  concurrencyButton.title = concurrencyButton.disabled ? '更新 VPS 网关后可调整处理槽位' : '';
  byId('metric-keys').textContent = fmt(keys.filter(key => key.enabled).length);
  byId('metric-keys-detail').textContent = '共 ' + fmt(keys.length) + ' 把密钥';
  byId('metric-requests-label').textContent = stats ? '近 24 小时请求' : '累计请求';
  byId('metric-requests').textContent = fmt(stats?.requests ?? keys.reduce((sum, key) => sum + key.requests, 0));
  byId('metric-requests-detail').textContent = stats
    ? '失败 ' + fmt(stats.failures) + ' · 受限 ' + fmt(stats.limited)
    : '消息请求总计';
  byId('metric-tokens-label').textContent = stats ? '近 24 小时 Token' : '累计 Token';
  const tokens = keys.every(key => key.tokens != null) ? keys.reduce((sum, key) => sum + key.tokens, 0) : null;
  byId('metric-tokens').textContent = fmt(stats?.tokens ?? tokens);
  byId('metric-cost').textContent = usd(snapshot.billing?.cost_nanos);
  byId('metric-tokens-detail').textContent = stats
    ? '近 1 分钟 ' + fmt(stats.rpm) + ' RPM · ' + fmt(stats.tpm) + ' TPM'
    : '输入、输出及缓存';
  byId('metric-queue').textContent = running + ' / ' + fmt(waiting);
  byId('metric-queue-detail').textContent = stats?.average_ms != null
    ? '近 24 小时平均处理 ' + duration(stats.average_ms)
    : '并发上限 ' + slotLabel(snapshot);
  const queue = byId('queue-strip');
  queue.hidden = false;
  queue.dataset.active = String(running > 0);
  byId('queue-current').textContent = running === 1 && current ? '正在处理 · ' + keyName(current)
    : running ? '正在处理 ' + fmt(running) + ' 个请求' : '网关空闲';
  byId('queue-capacity').max = slotProgressMaximum(snapshot);
  byId('queue-capacity').value = running;
  byId('queue-waiting').textContent = '并发 ' + running + '/' + slotLabel(snapshot) +
    ' · 排队 ' + fmt(waiting) + (snapshot.queue_limit == null ? '' : '/' + fmt(snapshot.queue_limit));
  byId('queue-waiting').title = snapshot.wait_limit == null ? '' : '最长排队等待 ' + snapshot.wait_limit + ' 秒';
}
function renderKeys() {
  const rows = byId('key-rows');
  const empty = byId('key-empty');
  const query = byId('key-search').value.trim().toLowerCase();
  const status = byId('key-filter').value;
  const sort = byId('key-sort').value;
  const keys = (snapshot?.keys ?? []).filter(key =>
    (!query || key.id.includes(query) || key.note.toLowerCase().includes(query)) &&
    (!status || key.enabled === (status === 'enabled')));
  keys.sort((left, right) => (right[sort] ?? 0) - (left[sort] ?? 0));
  const pages = Math.max(1, Math.ceil(keys.length / keysPerPage));
  keyPage = Math.min(keyPage, pages - 1);
  const visible = keys.slice(keyPage * keysPerPage, (keyPage + 1) * keysPerPage);
  byId('key-pagination').hidden = !keys.length;
  byId('key-count').textContent = '共 ' + fmt(keys.length) + ' 把密钥';
  byId('key-page').textContent = (keyPage + 1) + ' / ' + pages;
  byId('previous-keys').disabled = keyPage === 0;
  byId('next-keys').disabled = keyPage === pages - 1;
  const signature = JSON.stringify([visible, snapshot?.can_delete_revoked_keys]);
  if (signature !== keyRowsSignature) {
    keyRowsSignature = signature;
    const focused = document.activeElement?.closest('#key-rows tr');
    const focusIndex = focused ? [...focused.querySelectorAll('button')].indexOf(document.activeElement) : -1;
    const next = visible.map(key => {
      const row = element('tr');
      row.dataset.keyId = key.id;
      const name = element('strong', key.note || '未填写备注', 'key-name');
      const id = element('code', shortId(key.id) + '…');
      id.title = key.id;
      cell(row, name).append(id);
      const state = key.enabled ? pill('已启用', 'enabled') : pill('已撤销', 'revoked');
      const stateCell = cell(row, state);
      if (key.active || key.waiting) {
        const activity = [key.active && '执行中 ' + fmt(activeCount(key)), key.waiting && '排队 ' + fmt(key.waiting)].filter(Boolean).join(' · ');
        stateCell.append(element('small', activity, 'key-activity'));
      }
      cell(row, key.rpm ? fmt(key.rpm) + ' 次/分' : '不限频', 'number');
      const requests = cell(row, fmt(key.requests), 'number');
      if (key.failures != null) requests.title = '失败 ' + fmt(key.failures) + ' 次';
      cell(row, fmt(key.tokens), 'number');
      const cost = cell(row, costLabel(key), 'number');
      if (key.cost_nanos != null) {
        const remaining = key.budget_nanos == null ? null : key.budget_nanos - key.cost_nanos;
        const balance = element('small', remaining == null ? '不限额' : '余 ' + usd(remaining), 'key-balance');
        balance.dataset.exhausted = String(remaining != null && remaining <= 0);
        cost.append(balance);
      }
      cell(row, timestamp(key.last_used));
      const actions = element('div', null, 'row-actions');
      actions.append(action('用量详情', () => showUsage(key)));
      if (key.enabled) {
        actions.append(action('编辑', () => showEditor(key)), action('撤销', () => showKeyRemoval(key, false), 'revoke-link'));
      } else if (snapshot.can_delete_revoked_keys) {
        actions.append(action('彻底删除', () => showKeyRemoval(key, true), 'revoke-link'));
      }
      cell(row, actions);
      if (focused?.dataset.keyId === key.id && focusIndex >= 0) {
        requestAnimationFrame(() => {
          if (document.activeElement === document.body) actions.children[focusIndex]?.focus({preventScroll: true});
        });
      }
      return row;
    });
    rows.replaceChildren(...next);
  }
  empty.hidden = !snapshot || keys.length > 0;
  empty.textContent = snapshot?.keys.length ? '没有符合筛选条件的密钥。' : '还没有密钥。点击右上角创建第一把。';
}

const trafficChart = createGatewayLineChart(byId('traffic-chart'));
const errorChart = createGatewayLineChart(byId('error-chart'));
const periodNames = {'1h': '近 1 小时', '24h': '近 24 小时', '7d': '近 7 天'};
const clockLabel = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
const dayLabel = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit'});

function setDonut(node, parts, text) {
  let start = 0;
  const total = parts.reduce((sum, part) => sum + part.count, 0);
  const slices = parts.filter(part => part.count > 0).map(part => {
    const end = start + 100 * part.count / total;
    const slice = 'var(--chart-' + part.tone + ') ' + start + '% ' + end + '%';
    start = end;
    return slice;
  });
  node.style.backgroundImage = slices.length ? 'conic-gradient(' + slices.join(', ') + ')' : '';
  node.querySelector('strong').textContent = text;
  node.setAttribute('aria-label', parts.map(part => part.name + ' ' + fmt(part.count)).join('，'));
}

function renderModels() {
  const rows = byId('model-rows');
  const models = snapshot?.models ?? [];
  const modelTotal = models.reduce((total, model) => total + model.tokens, 0);
  const tones = ['mint', 'blue', 'violet', 'gold'];
  const modelParts = models.slice(0, tones.length).map((model, index) =>
    ({name: model.model, count: model.tokens, tone: tones[index]}));
  if (models.length > tones.length) {
    modelParts.push({name: '其他模型', count: modelTotal - modelParts.reduce((sum, part) => sum + part.count, 0), tone: 'gray'});
  }
  const share = byId('model-share');
  share.hidden = !modelTotal;
  setDonut(share, modelParts, compact.format(modelTotal));
  rows.replaceChildren(...models.map((model, index) => {
    const row = element('tr');
    const name = element('span', model.model, 'chart-swatch tone-' + (tones[index] ?? 'gray'));
    cell(row, name);
    cell(row, fmt(model.requests), 'number');
    cell(row, fmt(model.failures), 'number');
    cell(row, fmt(model.tokens), 'number');
    cell(row, costLabel(model), 'number');
    return row;
  }));
  const empty = byId('model-empty');
  empty.hidden = models.length > 0;
  empty.textContent = snapshot ? '暂无模型用量。' : '正在读取模型用量…';
}

function renderAnalyticsQueue() {
  const running = activeCount(snapshot);
  const waiting = snapshot?.waiting;
  const capacity = slotProgressMaximum(snapshot);
  const limit = snapshot?.queue_limit;
  byId('analysis-running').textContent = snapshot ? fmt(running) + ' / ' + slotLabel(snapshot) : '—';
  byId('analysis-waiting').textContent = limit == null ? fmt(waiting) : fmt(waiting) + ' / ' + fmt(limit);
  for (const [id, value, maximum] of [
    ['analysis-running-bar', running, snapshot && capacity], ['analysis-waiting-bar', waiting, limit]
  ]) {
    const bar = byId(id);
    bar.hidden = !(maximum > 0);
    if (maximum > 0) { bar.max = maximum; bar.value = value; }
  }
  const queued = (snapshot?.keys ?? []).filter(key => key.active || key.waiting)
    .sort((a, b) => activeCount(b) - activeCount(a) || b.waiting - a.waiting);
  const items = queued.slice(0, 4).map(key => {
    const li = element('li');
    const state = [key.active && '执行中 ' + fmt(activeCount(key)), key.waiting && '排队 ' + fmt(key.waiting)].filter(Boolean).join(' · ');
    li.append(element('span', keyName(key)), element('span', state));
    return li;
  });
  if (queued.length > items.length) items.push(element('li', '还有 ' + (queued.length - items.length) + ' 把密钥正在等待'));
  byId('analysis-queue').replaceChildren(...items);
  byId('analysis-queue-empty').textContent = items.length ? '' : snapshot ? '网关空闲' : '正在读取并发状态…';
  const policy = byId('analysis-queue-policy');
  policy.hidden = snapshot?.wait_limit == null;
  if (!policy.hidden) policy.textContent = '最长等待 ' + fmt(snapshot.wait_limit) + ' 秒';
}

function chartState(contentId, emptyId, visible, message) {
  byId(contentId).hidden = !visible;
  byId(emptyId).hidden = visible;
  byId(emptyId).textContent = visible ? '' : message;
}

function renderLatency(data, message) {
  const latency = data?.latency[byId('latency-metric').value];
  byId('latency-summary').textContent = latency?.samples
    ? '平均 ' + duration(latency.average_ms) + ' · P95 ' + duration(latency.p95_ms)
    : '';
  chartState('latency-bars', 'latency-empty', latency?.samples > 0, message || '这个时段暂无耗时样本。');
  if (!latency?.samples) return;
  const edges = data.latency_limits;
  const bucketLabel = ms => ms >= 1000 ? (ms / 1000) + ' 秒' : ms + ' 毫秒';
  const max = Math.max(...latency.histogram);
  byId('latency-bars').replaceChildren(...latency.histogram.map((count, index) => {
    const name = index === 0 ? '< ' + bucketLabel(edges[0]) :
      index === edges.length ? '≥ ' + bucketLabel(edges.at(-1)) :
      edges[index - 1] >= 1000 ? (edges[index - 1] / 1000) + '–' + bucketLabel(edges[index]) :
        edges[index - 1] + '–' + edges[index] + ' 毫秒';
    const item = element('div');
    item.setAttribute('role', 'listitem');
    const bar = element('progress');
    bar.max = max;
    bar.value = count;
    bar.setAttribute('aria-hidden', 'true');
    item.append(element('span', name), bar, element('span', fmt(count), 'number'));
    return item;
  }));
}

function renderOutcomes(data, message) {
  const chart = byId('outcome-chart');
  const resultRows = data?.outcomes ?? [];
  const total = resultRows.reduce((sum, row) => sum + row.requests, 0);
  chartState('outcome-chart', 'outcome-empty', total > 0, message || '这个时段暂无请求。');
  if (!total) return;
  const parts = Object.entries({success: 'mint', failed: 'rose', limited: 'gold', canceled: 'gray'}).map(([outcome, tone]) => {
    const rows = resultRows.filter(row => row.outcome === outcome);
    return {tone, name: outcomes[outcome], count: rows.reduce((sum, row) => sum + row.requests, 0),
      detail: rows.filter(row => row.status != null).map(row => 'HTTP ' + row.status + ' · ' + fmt(row.requests) + ' 条').join('；')};
  });
  const percent = new Intl.NumberFormat('zh-CN', {style: 'percent', maximumFractionDigits: 1});
  setDonut(chart.querySelector('.donut'), parts, percent.format(parts[0].count / total));
  chart.querySelector('ul').replaceChildren(...parts.map(part => {
    const li = element('li');
    li.title = part.detail;
    li.append(element('span', part.name, 'chart-swatch tone-' + part.tone), element('strong', fmt(part.count)));
    return li;
  }));
}

function renderAnalysis() {
  renderModels();
  renderAnalyticsQueue();
  const ready = snapshot && loadedAnalyticsPeriod === analyticsPeriod;
  const data = ready ? analyticsData : null;
  const message = ready ? data ? '' : '网关服务升级后可查看此图。' : '正在读取统计…';
  const buckets = new Map(data?.timeline.map(point => [point.bucket, point]));
  const points = data ? Array.from({length: data.count}, (_, index) =>
    buckets.get(index) ?? {requests: 0, tokens: 0, failed: 0, limited: 0}) : [];
  const totalRequests = points.reduce((sum, point) => sum + point.requests, 0);
  const totalTokens = points.reduce((sum, point) => sum + point.tokens, 0);
  byId('analysis-summary').textContent = data
    ? fmt(totalRequests) + ' 次请求 · ' + fmt(totalTokens) + ' Token'
    : ready ? '历史模型用量和实时并发仍可查看' : '正在读取' + periodNames[analyticsPeriod] + '的统计';
  renderLatency(data, message);
  renderOutcomes(data, message);

  const metric = byId('traffic-unit');
  const [field, kind] = metric.value.split('-');
  const unit = metric.selectedOptions[0].textContent;
  const labels = data ? points.map((_, index) => {
    const start = new Date((data.start + index * data.step) * 1000);
    const end = new Date(start.getTime() + data.step * 1000);
    return {short: data.step >= 86400 ? dayLabel.format(start) : clockLabel.format(start),
      full: dayLabel.format(start) + ' ' + clockLabel.format(start) + ' – ' + dayLabel.format(end) + ' ' + clockLabel.format(end)};
  }) : [];
  const historical = ready && !data && analyticsPeriod === '7d' ? snapshot.trend ?? [] : [];
  const haveTraffic = totalRequests > 0 || historical.length > 0;
  const haveErrors = Boolean(data && totalRequests);
  byId('traffic-summary').textContent = data ? periodNames[analyticsPeriod] + ' · 每段 ' +
    ({300: '5 分钟', 3600: '1 小时', 86400: '1 天'}[data.step]) : historical.length ? '每日汇总 · UTC' : '';
  chartState('traffic-chart', 'traffic-empty', haveTraffic, message || '这个时段暂无请求。');
  chartState('error-chart', 'error-empty', haveErrors, message || '这个时段暂无请求。');
  if (data && haveTraffic) {
    trafficChart(labels, [{tone: field === 'tokens' ? 'mint' : 'blue', label: unit, area: true, counts: kind === 'total',
      values: points.map(point => point[field] * (kind === 'rate' ? 60 / data.step : 1))}]);
  } else if (historical.length) {
    const daily = new Map(historical.map(point => [point.day, point]));
    const end = (snapshot.sampled_at ?? Date.now() / 1000) * 1000;
    const days = Array.from({length: 7}, (_, index) => new Date(end - (6 - index) * 86400000).toISOString().slice(0, 10));
    trafficChart(days.map(day => ({short: day.slice(5), full: day})), [{tone: 'mint', label: unit, area: true, counts: kind === 'total',
      values: days.map(day => (daily.get(day)?.[field] ?? 0) / (kind === 'rate' ? 1440 : 1))}]);
  }
  if (haveErrors) errorChart(labels, [
    {tone: 'rose', label: '失败', counts: true, values: points.map(point => point.failed)},
    {tone: 'gold', label: '受限', counts: true, values: points.map(point => point.limited)},
  ]);
}

async function loadSelectedAnalytics() {
  if (refreshTask) await refreshTask;
  if (activeView === 'analytics' && loadedAnalyticsPeriod !== analyticsPeriod) await refresh(false);
}

function renderRequests() {
  const filter = byId('request-filter').value;
  const data = keyHistory?.recent ?? snapshot?.recent;
  const entries = (data ?? []).filter(entry => !filter || entry.outcome === filter);
  const lookup = new Map((snapshot?.keys ?? []).map(key => [key.id, key]));
  byId('request-caption').textContent = keyHistory
    ? keyName(keyHistory.key) + ' · 最近 30 条'
    : '最近 30 条 · 新采集的请求';
  byId('clear-key-filter').hidden = !keyHistory;
  byId('request-rows').replaceChildren(...entries.map(entry => {
    const row = element('tr');
    const key = lookup.get(entry.key_id);
    cell(row, timestamp(entry.at));
    cell(row, key ? keyName(key) : shortId(entry.key_id));
    cell(row, entry.model);
    const result = pill(outcomes[entry.outcome] ?? entry.outcome, entry.outcome);
    if (entry.status) result.title = 'HTTP ' + entry.status;
    cell(row, result);
    cell(row, duration(entry.wait_ms), 'number');
    cell(row, duration(entry.duration_ms), 'number');
    cell(row, fmt(entry.tokens), 'number');
    return row;
  }));
  const empty = byId('request-empty');
  empty.hidden = entries.length > 0;
  empty.textContent = !snapshot ? '正在读取请求…' : !data ? '更新服务器上的 Safe Code API 后可查看请求明细。' :
    data.length ? '没有符合筛选条件的请求。' : '暂无新采集的请求记录。';
}

function selectView(view) {
  activeView = view;
  for (const button of viewButtons) {
    const selected = button.dataset.view === view;
    byId('view-' + button.dataset.view).hidden = !selected;
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  if (view === 'analytics') { renderAnalysis(); void loadSelectedAnalytics(); }
  if (view === 'requests') renderRequests();
}
function refresh(showProgress = true) {
  refreshTask ??= loadSnapshot(showProgress).finally(() => { refreshTask = undefined; });
  return refreshTask;
}
async function loadSnapshot(showProgress) {
  byId('refresh').disabled = true;
  if (showProgress) notice('status', '正在读取网关…');
  try {
    const fetchAnalytics = showProgress || loadedAnalyticsPeriod !== analyticsPeriod || Date.now() - analyticsRefreshedAt >= 10000;
    const period = activeView === 'analytics' && fetchAnalytics ? analyticsPeriod : undefined;
    snapshot = await manage('list', {details: true, background: !showProgress, ...(period && {analytics: period})});
    if (period) {
      loadedAnalyticsPeriod = period;
      analyticsData = snapshot.analytics;
      analyticsRefreshedAt = Date.now();
    }
    renderOverview();
    renderKeys();
    if (byId('key-editor').open) renderActivationField();
    if (activeView === 'analytics') renderAnalysis();
    if (activeView === 'requests') {
      const history = keyHistory;
      if (history) {
        const {recent} = await manage('usage', {id: history.key.id, background: !showProgress});
        if (keyHistory === history) history.recent = recent;
      }
      renderRequests();
    }
    byId('create-key').disabled = false;
    notice('status', '');
    return true;
  } catch (error) {
    renderClaudeSubscription(byId('gateway-claude-limits'), snapshot?.subscription
      ? {...snapshot.subscription, state: 'stale'} : undefined);
    notice('status', error.message, true);
    return false;
  } finally {
    byId('refresh').disabled = false;
  }
}
function renderActivationField() {
  const required = !editingKey && snapshot?.activation_required === true;
  const input = byId('activation-code');
  byId('activation-field').hidden = !required;
  input.disabled = !required;
  input.required = required;
  if (!required) input.value = '';
}
function showEditor(key) {
  editingKey = key;
  byId('editor-title').textContent = key ? '编辑密钥' : '创建密钥';
  byId('save-key').textContent = key ? '保存修改' : '创建密钥';
  byId('note').value = key?.note ?? '';
  byId('rpm').value = key?.rpm ?? 0;
  const budget = byId('budget');
  budget.disabled = !snapshot.billing;
  byId('budget-field').hidden = budget.disabled;
  budget.value = key?.budget_nanos == null ? '' : (key.budget_nanos / 1e9).toFixed(2);
  byId('budget-detail').textContent = key
    ? '已用 ' + costLabel(key) + '。提高累计额度可增加余额，留空不限额。'
    : '余额用完后暂停新请求，留空不限额。';
  renderActivationField();
  notice('editor-status', '');
  byId('key-editor').showModal();
  byId('note').focus();
}
function renderSettings(value) {
  settings = value;
  byId('settings-address-name').textContent = value.publicIp ? '公网 IP' : '监听 IP（服务器本地）';
  byId('settings-address').value = value.publicIp || value.bind;
  byId('settings-bind-field').hidden = !value.publicIp || value.publicIp === value.bind;
  byId('settings-bind').value = value.bind;
  byId('settings-port').value = value.port;
  byId('settings-origin').value = value.origin || '';
  byId('settings-origin').readOnly = !!value.developmentChannel;
  byId('settings-origin-hint').textContent = value.developmentChannel
    ? '示例环境 开发者渠道。新密钥自动使用上方的 FRP 公网入口。'
    : '创建密钥时使用此地址。外部地址或端口变更后，请重新签发密钥。';
  byId('settings-fields').hidden = false;
  updateSettingsButtons();
}
async function showSettings() {
  const dialog = byId('settings-dialog');
  const epoch = ++settingsEpoch;
  settings = undefined;
  byId('settings-fields').hidden = true;
  byId('save-settings').disabled = true;
  byId('save-origin').disabled = true;
  notice('settings-status', '正在读取网关设置…');
  dialog.showModal();
  try {
    const value = await manage('settings');
    if (dialog.open && epoch === settingsEpoch) {
      renderSettings(value);
      notice('settings-status', '');
    }
  } catch (error) {
    if (dialog.open && epoch === settingsEpoch) notice('settings-status', error.message, true);
  }
}
function showKeyRemoval(key, permanent) {
  keyRemoval = {key, permanent};
  const label = permanent ? '彻底删除' : '撤销密钥';
  byId('revoke-title').textContent = label;
  byId('revoke-key').textContent = label;
  byId('revoke-message').textContent = permanent
    ? '将永久删除「' + keyName(key) + '」及其用量和请求记录。此操作无法撤销。'
    : '撤销「' + keyName(key) + '」后，使用这把密钥的设备将无法发起新请求。';
  notice('revoke-status', '');
  byId('revoke-dialog').showModal();
  if (permanent) byId('revoke-dialog').querySelector('footer [data-close]').focus();
}
async function changeKey(task, statusId, button) {
  if (operationPending) return;
  operationPending = true;
  button.disabled = true;
  notice(statusId, '正在保存…');
  try {
    await task();
    await refreshTask;
    await refresh(false);
  } catch (error) {
    notice(statusId, error.message, true);
    if (statusId === 'editor-status' && !editingKey) {
      const activation = byId('activation-code');
      await refreshTask;
      await refresh(false);
      if (!activation.disabled) activation.focus();
    }
  } finally {
    operationPending = false;
    button.disabled = false;
  }
}
async function showUsage(key) {
  const epoch = ++usageEpoch;
  usageKey = key;
  const rows = byId('usage-rows');
  rows.replaceChildren();
  byId('usage-key').textContent = keyName(key);
  byId('view-key-requests').disabled = true;
  notice('usage-status', '正在读取用量…');
  const panel = byId('usage-dialog');
  panel.showModal();
  try {
    const {models, recent} = await manage('usage', {id: key.id});
    if (!panel.open || epoch !== usageEpoch) return;
    usageKey = {...key, recent};
    byId('view-key-requests').disabled = !Array.isArray(recent);
    notice('usage-status', !models.length ? '这把密钥暂无用量。' : '');
    rows.replaceChildren(...models.map(model => {
      const row = element('tr');
      cell(row, model.model);
      for (const value of [model.requests, model.failures, model.input, model.output, model.cache_read, model.cache_write, modelTokens(model)]) {
        cell(row, fmt(value), 'number');
      }
      cell(row, costLabel(model), 'number');
      return row;
    }));
  } catch (error) {
    if (panel.open && epoch === usageEpoch) notice('usage-status', error.message, true);
  }
}

for (const button of viewButtons) button.onclick = () => selectView(button.dataset.view);
const periodButtons = [...document.querySelectorAll('[data-period]')];
for (const button of periodButtons) button.onclick = () => {
  if (analyticsPeriod === button.dataset.period) return;
  analyticsPeriod = button.dataset.period;
  for (const item of periodButtons) item.setAttribute('aria-pressed', String(item === button));
  renderAnalysis();
  void loadSelectedAnalytics();
};
byId('traffic-unit').onchange = renderAnalysis;
byId('latency-metric').onchange = renderAnalysis;
byId('analysis-view-requests').onclick = () => {
  keyHistory = undefined;
  byId('request-filter').value = '';
  selectView('requests');
};
for (const button of document.querySelectorAll('[data-close]')) {
  button.onclick = () => { if (!operationPending) button.closest('dialog').close(); };
}
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('cancel', event => { if (operationPending) event.preventDefault(); });
}
byId('refresh').onclick = () => refresh();
byId('gateway-concurrency').onclick = () => {
  const input = byId('concurrency-value');
  input.value = snapshot.concurrency_text;
  notice('concurrency-status', '');
  byId('concurrency-dialog').showModal();
  input.focus();
};
byId('concurrency-form').onsubmit = async event => {
  event.preventDefault();
  const input = byId('concurrency-value');
  const button = byId('save-concurrency');
  if (operationPending || !input.reportValidity()) return;
  const concurrency = input.value;
  operationPending = true;
  button.disabled = input.disabled = true;
  notice('concurrency-status', '正在保存…');
  try {
    await refreshTask;
    await manage('set_concurrency', {concurrency});
    byId('concurrency-dialog').close();
    await refresh(false);
  } catch (error) {
    notice('concurrency-status', error.message, true);
  } finally {
    operationPending = false;
    button.disabled = input.disabled = false;
  }
};
byId('gateway-settings').onclick = showSettings;
function updateSettingsButtons() {
  const port = byId('settings-port');
  const origin = byId('settings-origin');
  byId('save-settings').disabled = operationPending || !settings || !port.validity.valid || port.valueAsNumber === settings.port;
  byId('save-origin').disabled = operationPending || !settings || origin.readOnly || !origin.validity.valid ||
    !origin.value.startsWith('https://') || origin.value === settings.origin;
}
byId('settings-port').oninput = updateSettingsButtons;
byId('settings-origin').oninput = updateSettingsButtons;
async function saveSetting(input, button, action, args, progress, success) {
  if (button.disabled) return;
  operationPending = true;
  byId('settings-port').disabled = byId('settings-origin').disabled = true;
  updateSettingsButtons();
  notice('settings-status', progress);
  try {
    await refreshTask;
    const otherInput = input.id === 'settings-origin' ? byId('settings-port') : byId('settings-origin');
    const pendingValue = otherInput.value;
    const savedValue = input.id === 'settings-origin' ? String(settings.port) : (settings.origin || '');
    renderSettings(await manage(action, args));
    if (pendingValue !== savedValue) otherInput.value = pendingValue;
    notice('settings-status', success);
  } catch (error) {
    notice('settings-status', error.message, true);
  } finally {
    operationPending = false;
    byId('settings-port').disabled = byId('settings-origin').disabled = false;
    updateSettingsButtons();
  }
}
byId('save-origin').onclick = () => {
  const input = byId('settings-origin');
  return saveSetting(input, byId('save-origin'), 'set_origin', {origin: input.value},
    '正在保存公网地址…', '公网地址已保存。');
};
byId('settings-form').onsubmit = event => {
  event.preventDefault();
  const input = byId('settings-port');
  return saveSetting(input, byId('save-settings'), 'set_port', {port: input.valueAsNumber, expectedPort: settings?.port},
    '正在保存并重启网关…', '已保存，网关正在新端口监听。');
};
function resetKeyPage() {
  keyPage = 0;
  renderKeys();
}
byId('key-search').oninput = resetKeyPage;
byId('key-filter').onchange = resetKeyPage;
byId('key-sort').onchange = resetKeyPage;
byId('previous-keys').onclick = () => { keyPage--; renderKeys(); };
byId('next-keys').onclick = () => { keyPage++; renderKeys(); };
byId('request-filter').onchange = renderRequests;
byId('clear-key-filter').onclick = () => {
  keyHistory = undefined;
  renderRequests();
};
byId('create-key').onclick = () => showEditor();
byId('key-form').onsubmit = event => {
  event.preventDefault();
  void changeKey(async () => {
    const options = {note: byId('note').value, rpm: byId('rpm').valueAsNumber};
    const budget = byId('budget');
    if (!budget.disabled) options.budget_cents = budget.value === '' ? null : Math.round(budget.valueAsNumber * 100);
    if (editingKey) {
      await manage('update', {...options, id: editingKey.id});
      byId('key-editor').close();
    } else {
      const activation = byId('activation-code');
      if (!activation.disabled) options.activation_code = activation.value;
      const result = await manage('issue', options);
      byId('key-editor').close();
      byId('ticket').value = result.ticket;
      notice('copy-status', '');
      byId('issued-dialog').showModal();
    }
  }, 'editor-status', byId('save-key'));
};
byId('key-editor').addEventListener('close', () => { byId('activation-code').value = ''; });
byId('issued-dialog').addEventListener('close', () => { byId('ticket').value = ''; });
byId('copy-key').onclick = async () => {
  try {
    await rpc('copy', {text: byId('ticket').value});
    notice('copy-status', '已复制密钥。');
  } catch (error) {
    notice('copy-status', error.message, true);
  }
};
byId('revoke-key').onclick = () => changeKey(async () => {
  const {key, permanent} = keyRemoval;
  await manage(permanent ? 'delete' : 'revoke', {id: key.id});
  if (permanent) {
    if (keyHistory?.key.id === key.id) keyHistory = undefined;
    if (usageKey?.id === key.id) usageKey = undefined;
    loadedAnalyticsPeriod = analyticsData = undefined;
  }
  byId('revoke-dialog').close();
}, 'revoke-status', byId('revoke-key'));
byId('view-key-requests').onclick = () => {
  keyHistory = {key: usageKey, recent: usageKey.recent};
  byId('request-filter').value = '';
  byId('usage-dialog').close();
  selectView('requests');
};

function scheduleAutoRefresh(delay = refreshIntervalMs) {
  clearTimeout(refreshTimer);
  if (!document.hidden) refreshTimer = setTimeout(autoRefresh, delay);
}
async function autoRefresh() {
  if (document.hidden) return;
  if (operationPending || document.querySelector('dialog[open]')) {
    scheduleAutoRefresh();
    return;
  }
  const success = await refresh(false);
  scheduleAutoRefresh(success ? refreshIntervalMs : retryIntervalMs);
}
document.addEventListener('visibilitychange', () => {
  scheduleAutoRefresh(0);
});

window.safeCodeNativeReady = true;
void refresh().then(success => scheduleAutoRefresh(success ? refreshIntervalMs : retryIntervalMs));
