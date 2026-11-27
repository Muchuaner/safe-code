"""Check CloudCLI password lookup in the shared desktop maintenance card."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
origin = 'https://safe-code.local/'
browser_path = os.environ.get('SAFECODE_TEST_BROWSER', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
account = {'state': 'verified', 'username': 'fixture-web', 'password': ' fixture <password> '}


def serve(route):
    if not route.request.url.startswith(origin):
        route.abort()
        return
    file = root / route.request.url.removeprefix(origin).split('?', 1)[0]
    if file.is_file():
        route.fulfill(path=str(file))
    else:
        route.fulfill(status=404, body='')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
    for platform in ('windows', 'macos'):
        page = browser.new_page(viewport={'width': 740, 'height': 700})
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', serve)
        page.add_init_script(f'window.__safeCodePlatform={json.dumps(platform)};' + '''
          window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;
          window.__safeCodeConnectionAcceptance=true;window.lookupCount=0;
          window.chrome={webview:{postMessage(m){
            let result=null;
            if(m.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
            if(m.op==='notice.state')result={accepted:true};
            if(m.op==='connect'||m.op==='cloud.install'){
              const server={osVersion:'24.04',cloudReady:m.op==='cloud.install',statuses:{account:true},assignedIps:[]};
              result=m.op==='connect'?{server}:server;
            }
            if(m.op==='cloud.login'){window.lookupCount++;window.lookup=m;return;}
            queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result}));
          }}};
          window.replyLogin=result=>window.safeCodeNativeResponse({id:window.lookup.id,result});
        ''')
        page.goto(origin + 'web/index.html')
        page.wait_for_function('window.safeCodeNativeReady')
        page.locator('#ip').fill('192.0.2.42')
        page.locator('#username').fill('root')
        page.locator('#password').fill('fixture')
        page.locator('#connect').click()
        expect(page.locator('#workspace-screen')).to_be_visible()
        page.locator('#maintenance-mode').click()

        install = page.locator('#install-cloud')
        query = page.locator('#cloud-login')
        dialog = page.locator('#app-message-dialog')
        body = page.locator('#app-message-body')
        # The installed web service can use a different systemd unit from the card's installer.
        expect(query).to_be_enabled()
        left, right = install.bounding_box(), query.bounding_box()
        assert abs(left['y'] - right['y']) <= 1
        assert abs(left['width'] - right['width']) <= 1 and left['x'] + left['width'] < right['x']
        assert page.evaluate('window.lookupCount') == 0

        for login, expected in ((account, '用户名 fixture-web'),
                                ({'state': 'unverified'}, '暂未通过 CloudCLI 验证'),
                                ({'state': 'unavailable'}, '没有可查询的 CloudCLI 登录信息')):
            query.click()
            expect(query).to_be_disabled()
            expect(query).to_have_text('查询中…')
            page.evaluate('window.replyLogin', login)
            expect(dialog).to_be_visible()
            expect(body).to_contain_text(expected)
            assert (account['password'] in body.text_content()) == (login == account)
            page.locator('#app-message-confirm').click()
            expect(body).to_be_empty()
            expect(query).to_be_enabled()

        query.click()
        page.locator('#disconnect-server').click()
        expect(page.locator('#login-screen')).to_be_visible()
        page.evaluate('window.replyLogin', account)
        expect(dialog).to_be_hidden()
        expect(body).to_be_empty()
        assert page.evaluate('window.lookupCount') == 4
        assert not errors, errors
        page.close()
    browser.close()

print('CLOUDCLI_LOGIN_DUAL_PLATFORM_LAYOUT_VERIFICATION_AND_STALE_RESPONSE_OK')
