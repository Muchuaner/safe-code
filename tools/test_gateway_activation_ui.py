"""Exercise the activation gate through a local browser with a mocked native bridge."""

from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_URL = 'https://safe-code.local/web/gateway.html'
CREATOR_URL = 'https://space.bilibili.com/49985881'

NATIVE_FIXTURE = """
window.issues = [];
window.updates = [];
window.ticketUpdates = [];
window.activeKeys = [];
window.concurrency = 1;
window.usageLookups = 0;
window.originMissing = false;
window.failModelCatalog = false;
window.addFixtureKey = () => {
  const key = {id: String(window.activeKeys.length + 1).padStart(64, '0'), note: '测试密钥',
    enabled: true, requests: 0, tokens: 0, rpm: 0, cost_nanos: 14616700000, unpriced_tokens: 10};
  window.activeKeys.push(key);
  return key;
};
window.chrome = {webview: {postMessage({id, op, args}) {
  let result, error;
  if (op !== 'safeCodeApi.manage') throw Error('Unexpected operation: ' + op);
  if (args.action === 'list') {
    result = {keys: window.activeKeys, can_delete_revoked_keys: true, key_policy_supported: true,
      activation_required: window.activeKeys.some(key => key.enabled),
      active: window.concurrency > 1, active_count: window.concurrency > 1 ? 2 : 0,
      concurrency: Number(window.concurrency), concurrency_text: String(window.concurrency),
      waiting: 0, billing: {cost_nanos: 0, unpriced_tokens: 10}};
  } else if (args.action === 'refresh_subscription') {
    window.usageLookups++;
    result = {state: 'available', windows: {five_hour: {used: 23.5}, seven_day: {used: 92}}};
  } else if (args.action === 'model_catalog') {
    if (window.failModelCatalog) error = '模拟目录读取失败。';
    else result = {models: [
      {id: 'fable', name: 'Fable 5'}, {id: 'sonnet', name: 'Sonnet 5.5'},
      {id: 'opus[1m]', name: 'Opus 5.5 (1M)'}, {id: 'haiku', name: 'Haiku 5.5'},
      {id: 'default', name: 'Default (recommended)'}, {id: 'best', name: 'Best available'},
      {id: 'opusplan', name: 'Opus Plan'}, {id: 'vendor/custom.model', name: 'Custom Model'}
    ]};
  } else if (args.action === 'set_concurrency') {
    window.concurrency = args.concurrency;
    result = {concurrency: window.concurrency};
  } else if (args.action === 'issue' && window.originMissing) {
    error = '未识别到 VPS 的公网 IP。请使用 VPS 公网 IP 重新建立 SSH 连接。';
  } else if (args.action === 'issue') {
    window.issues.push(args);
    if (window.activeKeys.some(key => key.enabled) && args.activation_code !== 'fixture-code') {
      error = '添加更多 API Key 需要正确的激活码。';
    } else {
      const key = Object.assign(window.addFixtureKey(), {note: args.note || '测试密钥',
        allowed_models: args.allowed_models, expires_at: args.expires_at});
      result = {...key, ticket: 'sc2.fixture'};
    }
  } else if (args.action === 'refresh_ticket') {
    window.ticketUpdates.push(args.ticket);
    result = {changed:true, ticket:'sc2.updated-fixture'};
  } else if (args.action === 'update') {
    window.updates.push(args);
    const key = window.activeKeys.find(key => key.id === args.id);
    Object.assign(key, {note: args.note, allowed_models: args.allowed_models, expires_at: args.expires_at,
      expired: args.expires_at != null && args.expires_at <= Date.now() / 1000});
    result = {id: args.id};
  } else if (args.action === 'delete') {
    window.activeKeys = window.activeKeys.filter(key => key.id !== args.id || key.enabled);
    result = {id: args.id};
  } else {
    throw Error('Unexpected gateway action: ' + args.action);
  }
  queueMicrotask(() => window.safeCodeNativeResponse({id, result, error}));
}}};
"""


def intercept(route):
    url = route.request.url
    if url == CREATOR_URL:
        route.fulfill(status=200, body='External link fixture')
    elif url in {f'https://safe-code.local/web/{name}' for name in
                 ('gateway.html', 'gateway.css', 'gateway.js', 'gateway-charts.js', 'gateway-status.js')}:
        route.fulfill(path=str(ROOT / 'web' / url.rsplit('/', 1)[-1]))
    else:
        route.abort()


def test_gateway_activation():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            headless=True,
        )
        try:
            page = browser.new_page(viewport={'width': 850, 'height': 600})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            page.route('**/*', intercept)
            page.add_init_script(NATIVE_FIXTURE)
            page.goto(GATEWAY_URL)
            create = page.locator('#create-key')
            save = page.locator('#save-key')
            editor = page.locator('#key-editor')
            field = page.locator('#activation-field')
            code = page.get_by_label('激活码', exact=True)
            issue_dialog = page.locator('#issued-dialog')

            quota = page.locator('#gateway-claude-limits')
            expect(quota.locator('[data-limit="five_hour"]')).to_have_text('76.5%')
            expect(quota.locator('[data-limit="seven_day"]')).to_have_text('8%')
            assert page.evaluate('window.usageLookups') == 1
            page.locator('#refresh').click()
            expect(page.locator('#refresh')).to_be_enabled()
            assert page.evaluate('window.usageLookups') == 1
            quota.get_by_role('button', name='查询 Claude 用量').click()
            page.wait_for_function('window.usageLookups === 2')

            slots = page.locator('#gateway-concurrency')
            expect(slots).to_have_text('处理槽位 1')
            slots.click()
            slot_value = page.locator('#concurrency-value')
            save_slots = page.locator('#save-concurrency')
            expect(slot_value).to_have_value('1')
            slot_value.fill('0')
            save_slots.click()
            expect(slots).to_have_text('处理槽位 不限')
            expect(page.locator('#queue-capacity')).to_be_hidden()
            slots.click()
            slot_value.fill('30')
            save_slots.click()
            expect(slots).to_have_text('处理槽位 30')
            expect(page.locator('#queue-waiting')).to_have_text('并发 2/30 · 排队 0')

            create.click()
            expect(field).to_be_hidden()
            expect(code).to_be_disabled()
            save.click()
            expect(issue_dialog).to_be_visible()
            assert page.evaluate('issues') == [{'action': 'issue', 'note': '', 'rpm': 0, 'budget_cents': None,
                                                'allowed_models': None, 'expires_at': None}]
            issue_dialog.get_by_role('button', name='完成').click()

            page.evaluate('window.originMissing = true')
            create.click()
            note = page.locator('#note')
            note.fill('保留草稿')
            code.fill('fixture-code')
            save.click()
            expect(page.locator('#editor-status')).to_contain_text('未识别到 VPS 的公网 IP')
            expect(page.locator('#settings-dialog, #editor-gateway-settings')).to_have_count(0)
            expect(note).to_have_value('保留草稿')
            expect(save).to_be_enabled()
            editor.get_by_role('button', name='取消').click()
            page.evaluate('window.originMissing = false')

            page.locator('#refresh-ticket').click()
            old_ticket = page.locator('#old-ticket')
            old_ticket.fill('sc1.old-fixture')
            page.locator('#save-ticket').click()
            expect(issue_dialog).to_be_visible()
            expect(issue_dialog.locator('#issued-title')).to_have_text('Key 已更新')
            expect(issue_dialog.locator('#ticket')).to_have_value('sc2.updated-fixture')
            expect(old_ticket).to_have_value('')
            assert page.evaluate('window.ticketUpdates') == ['sc1.old-fixture']
            issue_dialog.get_by_role('button', name='完成').click()


            create.click()
            expect(field).to_be_visible()
            expect(code).to_have_attribute('type', 'password')
            expect(code).to_have_attribute('required', '')
            expect(code).to_have_value('')
            link = editor.get_by_role('link', name='关注主播')
            expect(link).to_have_attribute('href', CREATOR_URL)
            save.click()
            assert page.evaluate('issues.length') == 1
            code.fill('wrong-fixture')
            save.click()
            expect(page.locator('#editor-status')).to_have_text('添加更多 API Key 需要正确的激活码。')
            expect(code).to_be_focused()
            expect(save).to_be_enabled()
            assert page.evaluate('issues.at(-1).activation_code') == 'wrong-fixture'
            code.fill('fixture-code')
            save.click()
            expect(issue_dialog).to_be_visible()
            assert page.evaluate('issues.at(-1).activation_code') == 'fixture-code'
            issue_dialog.get_by_role('button', name='完成').click()
            expect(code).to_have_value('')

            page.get_by_role('button', name='编辑', exact=True).first.click()
            expect(field).to_be_hidden()
            save.click()
            expect(editor).to_be_hidden()
            assert 'activation_code' not in page.evaluate('updates.at(-1)')

            expect(page.get_by_text('未计价')).to_have_count(0)
            expect(page.get_by_text('$14.6167', exact=False).first).to_be_visible()
            expect(page.get_by_role('button', name='彻底删除')).to_have_count(0)
            page.evaluate("Object.assign(window.addFixtureKey(), {note: '待删除密钥', enabled: false})")
            page.locator('#refresh').click()
            delete_row = page.get_by_role('row').filter(has_text='待删除密钥')
            delete_dialog = page.locator('#revoke-dialog')
            delete_row.get_by_role('button', name='彻底删除').click()
            expect(delete_dialog).to_contain_text('此操作无法撤销')
            cancel = delete_dialog.get_by_role('button', name='取消')
            expect(cancel).to_be_focused()
            cancel.click()
            expect(delete_row).to_be_visible()
            delete_row.get_by_role('button', name='彻底删除').click()
            delete_dialog.get_by_role('button', name='彻底删除').click()
            expect(delete_row).to_have_count(0)

            page.evaluate('window.activeKeys.forEach(key => key.enabled = false)')
            page.locator('#refresh').click()
            expect(page.locator('#metric-keys')).to_have_text('0')
            create.click()
            expect(field).to_be_hidden()
            page.evaluate('window.addFixtureKey()')
            save.click()
            expect(page.locator('#editor-status')).to_have_text('添加更多 API Key 需要正确的激活码。')
            expect(field).to_be_visible()
            expect(code).to_be_focused()
            assert 'activation_code' not in page.evaluate('issues.at(-1)')
            with page.expect_navigation(url=CREATOR_URL):
                link.click()
            assert not errors, errors
        finally:
            browser.close()


def test_key_policy(screenshots=None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        page = browser.new_page(viewport={'width': 1040, 'height': 760}, timezone_id='Asia/Shanghai', device_scale_factor=2)
        page.clock.set_fixed_time(datetime(2026, 11, 22, 6, tzinfo=timezone.utc))
        page.route('**/*', intercept)
        page.add_init_script(NATIVE_FIXTURE)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(GATEWAY_URL)

        def capture(name):
            if screenshots:
                page.mouse.move(4, 4)
                page.screenshot(path=screenshots/f'{name}.png', animations='disabled')

        create = page.locator('#create-key')
        save = page.locator('#save-key')
        editor = page.locator('#key-editor')
        model = page.get_by_label('模型管理', exact=True)
        expiry = page.get_by_label('失效时间', exact=True)
        date = page.locator('#key-expiry')
        create.click()
        expect(model).to_have_value('all')
        expect(expiry).to_have_value('never')
        expect(date).to_be_hidden()
        capture('01-key-defaults')
        page.locator('#note').fill('个人工作站')
        save.click()
        page.locator('#issued-dialog').get_by_role('button', name='完成').click()
        expect(page.get_by_role('row').filter(has_text='个人工作站')).to_contain_text('永不过期')

        page.evaluate("""Object.assign(addFixtureKey(), {note:'团队体验 Key'});
            Object.assign(addFixtureKey(), {note:'短期演示 Key', expires_at:1763769600, expired:true,
                allowed_models:['haiku']});window.failModelCatalog = true""")
        page.locator('#refresh').click()
        team = page.get_by_role('row').filter(has_text='团队体验 Key')
        team.get_by_role('button', name='编辑').click()
        model.select_option('selected')
        expect(page.locator('#model-status')).to_contain_text('模拟目录读取失败')
        save.click()
        expect(page.locator('#editor-status')).to_have_text('请至少选择一个允许使用的模型。')
        assert page.evaluate('updates.length') == 0
        page.evaluate('window.failModelCatalog = false')
        page.locator('#reload-models').click()
        page.locator('input[value="fable"]').check()
        page.locator('input[value="sonnet"]').check()
        expect(page.locator('input[value="vendor/custom.model"]')).to_have_count(1)
        expiry.select_option('scheduled')
        date.fill('2026-11-22T12:00')
        save.click()
        expect(page.locator('#editor-status')).to_have_text('请选择晚于现在的失效时间。')
        date.fill('2026-12-06T18:00')
        save.click()
        expect(editor).to_be_hidden()
        assert page.evaluate('updates.at(-1).allowed_models') == ['fable', 'sonnet']
        assert page.evaluate('updates.at(-1).expires_at') == int(datetime(2026, 12, 6, 10, tzinfo=timezone.utc).timestamp())
        expect(team).to_contain_text('2 个模型')
        expect(page.get_by_role('row').filter(has_text='短期演示 Key')).to_contain_text('已过期')
        capture('03-key-policy-list')
        team.get_by_role('button', name='编辑').click()
        expect(page.locator('input[value="sonnet"]')).to_be_checked()
        expect(date).to_have_value('2026-12-06T18:00')
        expect(page.locator('#model-status')).to_be_hidden()
        capture('02-key-restrictions')
        page.set_viewport_size({'width': 360, 'height': 600})
        assert editor.evaluate('node => node.scrollWidth <= node.clientWidth')
        expect(save).to_be_in_viewport()
        date.scroll_into_view_if_needed()
        expect(date).to_be_in_viewport()
        expect(save).to_be_in_viewport()
        capture('04-narrow-key-editor')
        page.keyboard.press('Escape')
        expect(editor).to_be_hidden()
        page.set_viewport_size({'width': 1040, 'height': 760})
        page.locator('#key-filter').select_option('expired')
        expired = page.get_by_role('row').filter(has_text='短期演示 Key')
        expect(expired).to_be_visible()
        expect(team).to_have_count(0)
        expired.get_by_role('button', name='编辑').click()
        expiry.select_option('never')
        model.select_option('all')
        save.click()
        expect(expired).to_have_count(0)
        assert page.evaluate('updates.at(-1).expires_at') is None
        assert page.evaluate('updates.at(-1).allowed_models') is None
        assert not errors, errors
        browser.close()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    test_gateway_activation()
    test_key_policy(args.screenshots)
    print('GATEWAY_ACTIVATION_AND_KEY_POLICY_OK')
