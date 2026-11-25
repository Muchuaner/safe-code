'use strict';

(() => {
  const main = document.querySelector('main');
  const heading = main.querySelector('.gateway-header h1');
  const bar = document.createElement('div');
  bar.className = 'mg-title';
  heading.replaceWith(bar);
  bar.append(heading);
  const selector = new Option('本机网关', 'local');
  const select = document.createElement('select');
  select.setAttribute('aria-label', '选择网关');
  select.add(selector);
  const gear = document.createElement('button');
  gear.type = 'button';
  gear.className = 'mg-gear';
  gear.setAttribute('aria-label', '多网关管理');
  gear.setAttribute('aria-haspopup', 'dialog');
  gear.textContent = '多网关';
  bar.append(select, gear);

  const panel = document.createElement('section');
  panel.className = 'mg-panel';
  panel.setAttribute('aria-label', '已绑定网关');
  panel.hidden = true;
  bar.closest('header').after(panel);
  const dialog = document.createElement('dialog');
  dialog.className = 'mg-dialog';
  dialog.setAttribute('aria-labelledby', 'mg-heading');
  dialog.innerHTML = `
    <header class="dialog-heading">
      <div><h2 id="mg-heading">多网关管理</h2><p class="muted mg-context" hidden></p></div>
      <button type="button" class="close" data-mg-close aria-label="关闭">×</button>
    </header>
    <section data-step="loading" class="dialog-content" hidden><p data-notice class="notice" role="status"></p></section>
    <section data-step="home" hidden>
      <div class="dialog-content mg-choices">
        <button type="button" data-next="create"><strong>生成绑定码</strong><small>使用激活码</small></button>
        <button type="button" data-next="pair"><strong>绑定设备</strong><small>使用另一台网关的绑定码</small></button>
      </div>
      <footer class="dialog-footer"><button type="button" data-mg-close>关闭</button></footer>
    </section>
    <form data-step="activate" hidden>
      <div class="dialog-content">
        <label>登记激活码<input name="code" type="password" maxlength="64" required autocomplete="off" spellcheck="false"></label>
        <label>网关备注<input name="name" maxlength="60" required></label>
        <label>服务器 IP<input name="address" required></label>
        <p data-notice class="notice" role="status" hidden></p>
      </div>
      <footer class="dialog-footer"><button type="button" data-mg-close>取消</button><button type="submit" class="primary">联网登记</button></footer>
    </form>
    <form data-step="create" hidden>
      <div class="dialog-content">
        <label>激活码<input name="code" type="password" maxlength="64" required autocomplete="off" spellcheck="false" placeholder="粘贴激活码"></label>
        <p data-notice class="notice" role="status" hidden></p>
      </div>
      <footer class="dialog-footer"><button type="button" data-next="home">返回</button><button type="submit" class="primary">核对激活码</button></footer>
    </form>
    <form data-step="review" hidden>
      <div class="dialog-content">
        <dl class="mg-terms">
          <div><dt>剩余使用次数</dt><dd data-uses></dd></div>
          <div><dt>绑定后管理期限</dt><dd data-duration></dd></div>
        </dl>
        <p data-activation-expiry class="muted"></p>
        <p class="muted">确认后消耗一次使用机会并生成绑定码。</p>
        <p data-notice class="notice" role="status" hidden></p>
      </div>
      <footer class="dialog-footer"><button type="button" data-next="create">返回</button><button type="submit" class="primary">确认生成绑定码</button></footer>
    </form>
    <section data-step="issued" hidden>
      <div class="dialog-content">
        <label>一次性绑定码<input class="mg-result" readonly type="text" spellcheck="false"></label>
        <p class="muted mg-issued-terms"></p>
        <p data-notice class="notice" role="status" hidden></p>
      </div>
      <footer class="dialog-footer"><button type="button" data-mg-close>完成</button><button type="button" class="primary mg-copy">复制绑定码</button></footer>
    </section>
    <form data-step="pair" hidden>
      <div class="dialog-content">
        <label>绑定码<input name="code" maxlength="64" required autocomplete="off" spellcheck="false" placeholder="粘贴绑定码"></label>
        <p data-notice class="notice" role="status" hidden></p>
      </div>
      <footer class="dialog-footer"><button type="button" data-next="home">返回</button><button type="submit" class="primary">绑定</button></footer>
    </form>`;
  main.append(dialog);
  const find = (query) => dialog.querySelector(query);
  let directory;
  let reviewToken = '';
  let needsEnrollment = false;
  let afterEnrollment = 'home';
  let busy = false;
  const steps = [...dialog.querySelectorAll('[data-step]')];
  const titles = {loading: '多网关管理', home: '多网关管理', activate: '登记网关', create: '生成绑定码',
    review: '确认使用激活码', issued: '绑定码已生成', pair: '绑定设备'};
  const remoteMatch = /^#gateway=([a-f0-9]{32})$/.exec(location.hash);
  const pageGateway = remoteMatch?.[1] || (location.hash === '#gateways' ? 'all' : 'local');

  async function call(action, data = {}) {
    const result = await rpc('safeCodeApi.manage', {action, ...data});
    if (result.error) {
      const error = new Error(result.error || '无法连接网关管理。');
      error.status = result.status;
      throw error;
    }
    return result.value;
  }

  function note(text, error = false) {
    const message = find('[data-step]:not([hidden]) [data-notice]');
    if (!message) return;
    message.textContent = text;
    message.dataset.error = String(error);
    message.hidden = !text;
  }

  function show(step) {
    for (const item of steps) item.hidden = item.dataset.step !== step;
    find('#mg-heading').textContent = titles[step];
    note('');
    find('[data-step]:not([hidden]) :is(input:not([readonly]), button, input)')?.focus();
  }

  function duration(days) { return days === null ? '永久' : days + ' 天'; }
  function card(gateway) {
    const item = document.createElement('article');
    const title = document.createElement('h2');
    title.textContent = gateway.name;
    const detail = document.createElement('p');
    detail.textContent = [gateway.address, gateway.online ? '在线' : '离线', gateway.last_seen
      ? '更新于 ' + new Date(gateway.last_seen * 1000).toLocaleString('zh-CN', {hour12:false}) : '等待同步'].join(' · ');
    const metrics = document.createElement('dl');
    const data = gateway.snapshot || {};
    const rows = [
      ['累计请求', data.requests], ['累计 Token', data.tokens],
      ['估算费用', data.cost_usd == null ? null : '$' + data.cost_usd.toFixed(4)],
      ['执行中', data.running], ['排队', data.queued],
    ];
    for (const [key, text] of [['five_hour','5 小时已用'], ['seven_day','7 天已用']]) {
      const quota = data.quota?.[key];
      if (quota && Date.now() < quota.resets_at * 1000) rows.push([text, Math.trunc(quota.used) + '%']);
    }
    for (const [label, value] of rows) {
      const cell = document.createElement('div');
      const name = document.createElement('dt');
      name.textContent = label;
      const count = document.createElement('dd');
      count.textContent = value == null ? '—' : typeof value === 'number' ? value.toLocaleString('zh-CN') : value;
      cell.append(name, count);
      metrics.append(cell);
    }
    item.append(title, detail, metrics);
    if (gateway.id !== directory.self.id) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '解除绑定';
      remove.addEventListener('click', async () => {
        if (!confirm('解除与「' + gateway.name + '」的绑定？')) return;
        remove.disabled = true;
        try {
          await call('multi.remove', {gateway: gateway.id});
          await load();
        } catch (error) {
          remove.disabled = false;
          detail.textContent = error.message;
        }
      });
      const actions = document.createElement('div');
      actions.className = 'mg-actions';
      actions.append(remove);
      item.append(actions);
    }
    return item;
  }

  function display() {
    if (!directory) return;
    const selected = directory.children.find(gateway => gateway.id === select.value);
    const summary = select.value === 'all' || (selected && selected.scope !== 'manage');
    panel.hidden = !summary;
    main.classList.toggle('mg-remote', Boolean(summary));
    if (!summary) return;
    const list = select.value === 'all'
      ? [directory.self, ...directory.children]
      : directory.children.filter((gateway) => gateway.id === select.value);
    const cards = list.map(card);
    if (select.value === 'all') {
      const total = document.createElement('p');
      total.textContent = '已登记及绑定 ' + list.length + ' 台 · 在线 ' + list.filter(x => x.online).length + ' 台';
      cards.unshift(total);
    }
    panel.replaceChildren(...cards);
  }

  async function load() {
    directory = await call('multi.list');
    select.replaceChildren(
      new Option('本机网关 · ' + directory.self.address, 'local'),
      ...(directory.children.length ? [new Option('全部网关', 'all')] : []),
      ...directory.children.map((entry) => new Option(entry.name + ' · ' + entry.address, entry.id)),
    );
    if ([...select.options].some(option => option.value === pageGateway)) {
      select.value = pageGateway;
    } else if (pageGateway !== 'local') {
      const unavailable = new Option('当前网关授权已失效', pageGateway);
      unavailable.disabled = true;
      select.add(unavailable);
      select.value = pageGateway;
    }
    display();
  }

  function openGateway(gateway) {
    const hash = gateway === 'local' ? '' : gateway === 'all' ? 'gateways' : 'gateway=' + gateway;
    location.hash = hash;
    location.reload();
  }

  function submit(step, work) {
    const form = find('[data-step="' + step + '"]');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy) return;
      const button = form.querySelector('[type="submit"]');
      busy = true;
      button.disabled = true;
      note('正在验证…');
      try { await work(form); } catch (error) { note(error.message, true); }
      finally { busy = false; button.disabled = false; }
    });
  }
  select.addEventListener('change', () => openGateway(select.value));
  gear.addEventListener('click', async () => {
    afterEnrollment = 'home';
    needsEnrollment = false;
    show('loading');
    dialog.showModal();
    note('正在读取授权状态…');
    try {
      await load();
      if (!dialog.open) return;
      const context = find('.mg-context');
      context.textContent = '操作网关 · 本机 ' + directory.self.address;
      context.hidden = false;
      show('home');
    } catch (error) {
      if (error.status === 401) {
        needsEnrollment = true;
        show('home');
      } else {
        const older = Number.isInteger(snapshot?.gatewayRevision) && snapshot.gatewayRevision < 29;
        note(older ? '请先回到网关设置，点击「更新网关」，再打开多网关管理。' : error.message, true);
      }
    }
  });

  for (const button of dialog.querySelectorAll('[data-next]')) {
    button.addEventListener('click', () => {
      if (!busy) {
        reviewToken = '';
        if (needsEnrollment && ['create', 'pair'].includes(button.dataset.next)) {
          afterEnrollment = button.dataset.next;
          show('activate');
          note('首次使用前，请先用登记激活码登记本机网关。');
        } else show(button.dataset.next);
      }
    });
  }
  for (const button of dialog.querySelectorAll('[data-mg-close]')) {
    button.addEventListener('click', () => { if (!busy) dialog.close(); });
  }
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });

  submit('activate', async form => {
    await call('multi.activate', Object.fromEntries(new FormData(form)));
    form.reset();
    await load();
    const context = find('.mg-context');
    context.textContent = '操作网关 · 本机 ' + directory.self.address;
    context.hidden = false;
    needsEnrollment = false;
    show(afterEnrollment);
  });
  submit('create', async form => {
    const result = await call('multi.inspect', {code: form.elements.code.value.trim()});
    reviewToken = result.review;
    form.reset();
    find('[data-uses]').textContent = result.uses_remaining === null ? '不限次数' : result.uses_remaining + ' 次';
    find('[data-duration]').textContent = duration(result.days);
    const expiry = find('[data-activation-expiry]');
    expiry.hidden = result.activation_expires === null;
    if (!expiry.hidden) {
      expiry.textContent = '激活码有效至 ' + new Date(result.activation_expires * 1000).toLocaleString('zh-CN', {hour12:false});
    }
    show('review');
  });
  submit('review', async () => {
    const result = await call('multi.create', {review: reviewToken});
    reviewToken = '';
    find('.mg-result').value = result.code;
    find('.mg-issued-terms').textContent = '绑定码 10 分钟内可使用一次。绑定后管理权限有效期为' + duration(result.days) + '。';
    show('issued');
  });
  find('.mg-copy').addEventListener('click', async () => {
    const field = find('.mg-result');
    try {
      await rpc('copy', {text: field.value});
      note('已复制绑定码。');
    } catch {
      field.select();
      note('已选中绑定码，请手动复制。');
    }
  });
  submit('pair', async form => {
    const result = await call('multi.claim', {code: form.elements.code.value.trim()});
    dialog.close();
    openGateway(result.id);
  });
  dialog.addEventListener('close', () => {
    find('.mg-result').value = '';
    find('.mg-context').hidden = true;
    for (const form of dialog.querySelectorAll('form')) form.reset();
    reviewToken = '';
  });
  if (location.hash === '#directory') gear.click();
  else load().catch(() => {});
  setInterval(() => { if (directory && document.visibilityState === 'visible') load().catch(() => {}); }, 30000);
})();
