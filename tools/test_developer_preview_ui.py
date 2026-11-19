#!/usr/bin/env python3
"""Verify that real logins start directly and developer preview stays offline."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':740,'height':634});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(request):
        url=request.request.url
        if not url.startswith('https://safe-code.local/'):request.abort();return
        file=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        if file.is_file():request.fulfill(path=str(file))
        else:request.fulfill(status=404,body='')
    page.route('**/*',route)
    page.goto('https://safe-code.local/index.html')
    assert page.locator('#notice-screen').is_visible()
    assert not page.locator('[id*="guide-"]').count()
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.calls=[];
      window.chrome={webview:{postMessage(message){
        window.calls.push(message);
        let result=null;
        if(message.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
        if(message.op==='notice.state')result={accepted:false};
        if(message.op==='connect')result={failure:'fixture refused connection'};
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.reload();page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#accept').click()
    page.locator('#ip').fill('192.0.2.5')
    page.locator('#username').fill('fixture')
    page.locator('#password').fill('fixture-only')
    page.locator('#connect').click()
    page.wait_for_function("window.calls.some(x=>x.op==='connect'&&x.args.host==='192.0.2.5')")
    assert not page.locator('[id*="guide-"]').count()
    page.reload();page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#accept').click();page.locator('#ip').fill('127.0.0.1');page.locator('#connect').click()
    page.wait_for_selector('#developer-preview')
    assert page.locator('#workspace-screen').is_visible()
    assert not page.locator('[id*="guide-"]').count()
    assert page.locator('[data-resource=qq]').get_attribute('href')=='https://qm.qq.com/q/Bzd5aqxW36'
    assert '国内直连' not in page.content()
    page.locator('#maintenance-mode').click();assert page.locator('#maintenance-panel').is_visible()
    page.locator('#terminal-card').click();page.wait_for_selector('#app-message-dialog[open]')
    assert '开发预览' in page.locator('#app-message-dialog').inner_text()
    calls=page.evaluate('window.calls.map(x=>x.op)');assert 'preview.begin' in calls
    assert not set(calls)&{'connect','deploy','metrics','ip.info','terminal','account.draft.save'},calls
    page.locator('#app-message-dialog .close').click()
    output=root/'.local/evidence';output.mkdir(exist_ok=True,parents=True)
    page.screenshot(path=str(output/'developer-preview.png'))
    assert not errors,errors
    browser.close()
print('DIRECT_LOGIN_AND_OFFLINE_DEVELOPER_PREVIEW_PASSED')
