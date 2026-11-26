'use strict';

function renderClaudeSubscription(container, subscription) {
  const state = subscription?.state;
  container.dataset.state = state ?? 'unavailable';
  const details = [];
  for (const [name, label] of [['five_hour', '5 小時'], ['seven_day', '7 天']]) {
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
    details.push(label + '剩餘 ' + display + (at ? '，重設於 ' + at : ''));
  }
  container.title = details.length
    ? (state === 'stale' ? '上次讀取 · ' : '') + details.join(' · ')
    : '點擊重新整理查詢網關所用 Claude 帳戶的訂閱用量';
}

function bindClaudeUsageRefresh(container, lookup, accept) {
  const button = container.querySelector('.quota-refresh');
  async function refresh() {
    if (button.disabled) return;
    button.disabled = true;
    button.title = '正在查詢…';
    try {
      const value = await lookup();
      accept(value);
      if (value?.state !== 'available') {
        button.title = value?.reason || 'Claude Code 未返回最新額度，請稍後重試';
      } else {
        button.title = '查詢 Claude 用量';
      }
    } catch (error) {
      button.title = error.message;
    } finally {
      button.disabled = false;
    }
  }
  button.addEventListener('click', refresh);
  return refresh;
}
