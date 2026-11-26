"""Shared Win/Mac UI gate backed by per-connection, explicitly saved settings."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]

BRIDGE = """
window.__safeCodeDesktop = true;
window.__safeCodeAcceptance = true;
window.calls = [];
window.preferences = {};
window.failLocaleSave = false;
window.chrome = {webview: {postMessage({id, op, args}) {
  window.calls.push({op, args});
  let result = null;
  let error;
  const settings = () => window.preferences[window.host] ??= {accountSaved: false, localeRegion: null};
  if (op === 'services.preferences') result = {enabled: false, serviceAvailable: false, confirmed: true};
  if (op === 'notice.state') result = {accepted: true};
  if (op === 'connect') {
    window.host = args.host;
    window.server = {osId: 'ubuntu', osVersion: '24.04', hostname: 'existing-host', username: 'workspace',
      statuses: {account: true, localization: true}, timezone: 'America/New_York', systemLocale: 'en_US.UTF-8',
      homeDirectory: '/home/workspace', observedEgressIp: args.host, assignedIps: [args.host, '9.9.9.9']};
    result = {server: window.server};
  }
  if (op === 'deployment.settings.get') result = {...settings()};
  if (op === 'account.draft.get') result = settings().draft;
  if (op === 'account.change.status') result = {requiresConfirmation: true};
  if (op === 'account.draft.save') {
    Object.assign(settings(), {accountSaved: true, draft: args});
    result = true;
  }
  if (op === 'deployment.locale.save') {
    if (window.failLocaleSave) error = '測試儲存失敗';
    else {
      settings().localeRegion = args.region;
      result = {...settings()};
    }
  }
  if (op === 'ip.info') {
    result = {location: '美國 · 紐約', regionCode: 'us', city: 'New York', score: 80, geoSource: '測試'};
  }
  if (op === 'ip.apply') {
    window.server.observedEgressIp = args.egressIp;
    result = window.server;
  }
  if (op === 'deploy') {
    window.server.statuses[args.step] = true;
    if (args.step === 'account') delete settings().draft;
    if (args.step === 'localization') Object.assign(window.server, {timezone: args.locale.timezone, systemLocale: args.locale.locale});
    result = window.server;
  }
  queueMicrotask(() => window.safeCodeNativeResponse({id, result, error}));
}}};
"""


def test_manual_deployment():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            headless=True,
        )
        try:
            for platform in ('windows', 'macos'):
                page = browser.new_page(viewport={'width': 850, 'height': 680})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def route(request):
                    prefix = 'https://safe-code.local/'
                    if not request.request.url.startswith(prefix):
                        request.abort()
                        return
                    path = ROOT / request.request.url.removeprefix(prefix).split('?', 1)[0]
                    if path.is_file():
                        request.fulfill(path=str(path))
                    else:
                        request.fulfill(status=404, body='')

                page.route('**/*', route)
                page.add_init_script(f'window.__safeCodePlatform = "{platform}";' + BRIDGE)
                page.goto('https://safe-code.local/web/index.html')
                page.wait_for_function('window.safeCodeNativeReady')

                def connect(ip):
                    page.evaluate("showScreen('login')")
                    page.locator('#ip').fill(ip)
                    page.locator('#username').fill('root')
                    page.locator('#password').fill('fixture-password-only')
                    page.locator('#connect').click()
                    expect(page.locator('#ip-location')).to_have_text('美國 · 紐約')

                def assert_all_locked():
                    row_deployments = page.locator('#steps .deploy-button:not(.locale-settings-button)')
                    assert row_deployments.count() == 7
                    assert row_deployments.evaluate_all('(buttons) => buttons.every(button => button.disabled)')
                    expect(page.locator('#deploy-all')).to_be_disabled()

                locale_button = page.get_by_role('button', name='自訂本土化', exact=True)
                account_button = page.get_by_role('button', name='自訂工作用戶和機器名稱', exact=True)
                locale = page.locator('#locale-region')
                locale_save = page.locator('#localization-form [type=submit]')
                batch = page.locator('#deploy-all')
                connect('1.1.1.1')
                assert_all_locked()
                expect(page.locator('#overall-status')).to_have_text('2 / 7 已完成')
                expect(page.locator('#overall-detail')).to_have_text('請先分別開啟前兩項「自訂」並儲存設定。')
                locale_button.click()
                expect(locale).to_have_value('')
                expect(locale_save).to_be_disabled()
                assert '自動' not in locale.inner_text()
                locale.select_option('us_ny')
                page.locator('#close-localization').click()
                locale_button.click()
                expect(locale).to_have_value('')
                page.locator('#close-localization').click()

                account_button.click()
                expect(page.locator('#custom-hostname')).to_have_value('existing-host')
                page.locator('#account-settings-form [type=submit]').click()
                page.get_by_role('button', name='確認並儲存參數', exact=True).click()
                expect(page.locator('#account-settings')).to_be_hidden()
                assert_all_locked()

                page.evaluate('window.failLocaleSave = true')
                locale_button.click()
                locale.select_option('jp')
                locale_save.click()
                expect(page.locator('#app-message-body')).to_have_text('測試儲存失敗')
                page.locator('#app-message-confirm').click()
                expect(locale).to_have_value('jp')
                expect(locale_save).to_be_enabled()
                assert_all_locked()
                page.evaluate('window.failLocaleSave = false')
                locale_save.click()
                expect(page.locator('#localization-dialog')).to_be_hidden()
                expect(batch).to_be_enabled()
                expect(page.locator('#steps .step-copy p').nth(1)).to_contain_text('Asia/Tokyo')
                assert not page.evaluate("window.calls.some(call => call.op === 'deploy')")

                batch.click()
                expect(page.get_by_role('checkbox', name='選擇更新系統並設定工作用戶')).to_be_disabled()
                page.get_by_role('checkbox', name='選擇本土化設定').check()
                page.get_by_role('checkbox', name='選擇安裝 Chrome').check()
                expect(batch).to_have_text('開始部署（2 項）')
                batch.click()
                page.wait_for_function("window.calls.filter(call => call.op === 'deploy').length === 2")
                assert page.evaluate("window.calls.filter(call => call.op === 'deploy').map(call => call.args.step)") == [
                    'localization', 'chrome',
                ]
                expect(page.locator('#steps .step-copy p').nth(1)).to_contain_text('部署完成')
                page.get_by_role('button', name='更新系統並設定工作用戶：更新', exact=True).click()
                expect(page.get_by_role('button', name='更新系統並設定工作用戶：已完成', exact=True)).to_be_disabled()

                page.locator('#maintenance-mode').click()
                page.locator('#change-ips').click()
                page.locator('#egress-ip-trigger').click()
                page.get_by_role('option', name='9.9.9.9', exact=True).click()
                page.locator('#ip-policy-form [type=submit]').click()
                expect(page.locator('#ip-policy-dialog')).to_be_hidden()
                expect(page.locator('#server-address')).to_have_text('9.9.9.9')
                page.locator('#deployment-mode').click()
                expect(batch).to_be_enabled()
                locale_button.click()
                expect(locale).to_have_value('jp')
                page.locator('#close-localization').click()

                connect('8.8.8.8')
                assert_all_locked()
                locale_button.click()
                expect(locale).to_have_value('')
                page.locator('#close-localization').click()
                connect('1.1.1.1')
                expect(batch).to_be_enabled()
                locale_button.click()
                expect(locale).to_have_value('jp')
                page.locator('#close-localization').click()
                assert not errors, errors
                page.close()
        finally:
            browser.close()


if __name__ == '__main__':
    test_manual_deployment()
    print('MAC_AND_WINDOWS_MANUAL_DEPLOYMENT_GATE_AND_TRUSTED_SETTINGS_RESTORE_OK')
