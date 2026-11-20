"""Exercise the activation gate through a local browser with a mocked native bridge."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_URL = 'https://safe-code.local/web/gateway.html'
CREATOR_URL = 'https://space.bilibili.com/49985881'

NATIVE_FIXTURE = """
window.issues = [];
window.updates = [];
window.activeKeys = [];
window.concurrency = 1;
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
    result = {keys: window.activeKeys, can_delete_revoked_keys: true,
      activation_required: window.activeKeys.some(key => key.enabled),
      active: window.concurrency > 1, active_count: window.concurrency > 1 ? 2 : 0,
      concurrency: Number(window.concurrency), concurrency_text: String(window.concurrency),
      waiting: 0, billing: {cost_nanos: 0, unpriced_tokens: 10}};
  } else if (args.action === 'set_concurrency') {
    window.concurrency = args.concurrency;
    result = {concurrency: window.concurrency};
  } else if (args.action === 'issue') {
    window.issues.push(args);
    if (window.activeKeys.some(key => key.enabled) && args.activation_code !== 'fixture-code') {
      error = '添加更多 API Key 需要正确的激活码。';
    } else {
      result = {...window.addFixtureKey(), ticket: 'sc2.fixture'};
    }
  } else if (args.action === 'update') {
    window.updates.push(args);
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

            def intercept(route):
                url = route.request.url
                if url == CREATOR_URL:
                    route.fulfill(status=200, body='External link fixture')
                elif url in {f'https://safe-code.local/web/{name}' for name in
                             ('gateway.html', 'gateway.css', 'gateway.js', 'gateway-charts.js', 'gateway-status.js')}:
                    route.fulfill(path=str(ROOT / 'web' / url.rsplit('/', 1)[-1]))
                else:
                    route.abort()

            page.route('**/*', intercept)
            page.add_init_script(NATIVE_FIXTURE)
            page.goto(GATEWAY_URL)
            create = page.locator('#create-key')
            save = page.locator('#save-key')
            editor = page.locator('#key-editor')
            field = page.locator('#activation-field')
            code = page.get_by_label('激活码', exact=True)
            issue_dialog = page.locator('#issued-dialog')

            slots = page.locator('#gateway-concurrency')
            expect(slots).to_have_text('处理槽位 1')
            slots.click()
            slot_value = page.locator('#concurrency-value')
            save_slots = page.locator('#save-concurrency')
            expect(slot_value).to_have_value('1')
            slot_value.fill('0')
            save_slots.click()
            assert page.evaluate('window.concurrency') == 1
            slot_value.fill('30')
            save_slots.click()
            expect(slots).to_have_text('处理槽位 30')
            expect(page.locator('#queue-waiting')).to_have_text('并发 2/30 · 排队 0')

            create.click()
            expect(field).to_be_hidden()
            expect(code).to_be_disabled()
            save.click()
            expect(issue_dialog).to_be_visible()
            assert page.evaluate('issues') == [{'action': 'issue', 'note': '', 'rpm': 0, 'budget_cents': None}]
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


if __name__ == '__main__':
    test_gateway_activation()
    print('GATEWAY_ACTIVATION_FIRST_KEY_MORE_KEYS_REVOKED_RACE_AND_CREATOR_LINK_OK')
