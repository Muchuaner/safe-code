'use strict';

function renderClaudeSubscription(container, subscription) {
  const state = subscription?.state;
  container.dataset.state = state ?? 'unavailable';
  const details = [];
  for (const [name, label] of [['five_hour', '5 小时'], ['seven_day', '7 天']]) {
    const usage = subscription?.windows?.[name];
    const reset = usage?.resets_at;
    const percent = Number.isFinite(usage?.used) && (!reset || reset * 1000 > Date.now())
      ? Math.max(0, Math.min(100, 100 - usage.used)) : null;
    const value = container.querySelector('[data-limit="' + name + '"]');
    value.parentElement.hidden = percent == null;
    if (percent == null) continue;
    const display = percent.toLocaleString('zh-CN', {maximumFractionDigits: 1}) + '%';
    value.textContent = display;
    value.dataset.low = String(percent <= 15);
    const at = reset ? new Date(reset * 1000).toLocaleString('zh-CN', {hour12: false}) : '';
    details.push(label + '剩余 ' + display + (at ? '，重置于 ' + at : ''));
  }
  container.title = details.length
    ? (state === 'stale' ? '上次读取 · ' : '') + details.join(' · ')
    : '点击刷新查询网关所用 Claude 账户的订阅用量';
}

function bindClaudeUsageRefresh(container, lookup, accept) {
  const button = container.querySelector('.quota-refresh');
  const link = container.querySelector('.quota-link');
  async function refresh() {
    if (button.disabled) return;
    button.disabled = true;
    button.title = '正在查询…';
    link.hidden = true;
    try {
      const value = await lookup();
      accept(value);
      if (value?.state !== 'available') {
        button.title = value?.reason || 'Claude Code 未返回最新额度，可在官方用量页查看';
        link.hidden = false;
      } else {
        button.title = '查询 Claude 用量';
      }
    } catch (error) {
      button.title = error.message;
      link.hidden = false;
    } finally {
      button.disabled = false;
    }
  }
  button.addEventListener('click', refresh);
  return refresh;
}
