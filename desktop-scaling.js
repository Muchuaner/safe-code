/* Optional Windows desktop presentation module. Settings remain local to this PC. */
window.safeCodeDesktopScaling = (() => {
  async function initialize({native, isMac, showError}) {
    const card = document.getElementById('desktop-scaling-card');
    if (!card || isMac) return;
    const toggle = document.getElementById('desktop-scaling-toggle');
    const state = document.getElementById('desktop-scaling-state');
    const settings = document.getElementById('desktop-scaling-settings');
    let enabled = false;
    let busy = true;
    const render = () => {
      toggle.setAttribute('aria-checked', String(enabled));
      toggle.disabled = busy;
      state.textContent = busy ? '正在读取…' : enabled ? '已开启' : '已关闭';
    };
    card.hidden = false;
    render();
    settings.onclick = () => document.getElementById('desktop-resolution')?.click();
    try {
      enabled = !!(await native('desktop.scaling.get')).enabled;
      busy = false;
      render();
    } catch {
      state.textContent = '当前版本暂不可用';
      return;
    }
    toggle.onclick = async () => {
      if (busy) return;
      busy = true;
      toggle.disabled = true;
      state.textContent = '正在保存…';
      try {
        enabled = !!(await native('desktop.scaling.save', {enabled: !enabled})).enabled;
      } catch (error) {
        await showError(error.message);
      } finally {
        busy = false;
        render();
      }
    };
  }
  return Object.freeze({initialize});
})();
