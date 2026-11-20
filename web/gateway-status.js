'use strict';

function renderClaudeSubscription(container, subscription) {
  const state = subscription?.state;
  const messages = {
    loading: '正在读取 VPS 上 Claude 订阅的用量',
    unavailable: 'VPS 上的 Claude 登录未提供订阅用量',
    unreachable: '暂时无法读取 Claude 订阅用量',
    rate_limited: 'Claude 用量查询暂时受限',
    stale: 'Claude 用量暂未更新，显示上次读取的数据',
  };
  container.dataset.state = state ?? 'unavailable';
  container.title = messages[state] ?? 'VPS 上 Claude 订阅的剩余额度';
  const details = [container.title];
  for (const [name, label] of [['five_hour', '5 小时'], ['seven_day', '7 天']]) {
    const usage = subscription?.windows?.[name];
    const reset = usage?.resets_at;
    const expired = state === 'stale' && reset && reset * 1000 <= Date.now();
    const percent = usage && !expired ? Math.max(0, Math.min(100, 100 - usage.used)) : null;
    const value = container.querySelector('[data-limit="' + name + '"]');
    const display = percent == null ? '—' : percent.toLocaleString('zh-CN', {maximumFractionDigits: 1}) + '%';
    value.textContent = display;
    value.dataset.low = String(percent != null && percent <= 15);
    const at = reset ? new Date(reset * 1000).toLocaleString('zh-CN', {hour12: false}) : '';
    details.push(label + '剩余 ' + display + (at ? '，重置于 ' + at : ''));
  }
  container.title = details.join(' · ');
  container.setAttribute('aria-label', details.join('，'));
}
