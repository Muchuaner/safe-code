#!/usr/bin/env python3
"""验证指南关闭、独立窗口桥接及不访问服务器的开发预览。"""
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
    guide=page.locator('#guide-dialog');assert not guide.is_visible(),'Closed guide remains visible'
    page.locator('#notice-guide-link').click();assert guide.is_visible()
    page.locator('#close-guide').click();assert not guide.is_visible()
    page.locator('#notice-guide-link').click();page.locator('#guide-done-btn').click();assert not guide.is_visible()
    page.locator('#notice-guide-link').click();page.keyboard.press('Escape');assert not guide.is_visible()
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.calls=[];
      window.chrome={webview:{postMessage(message){
        window.calls.push(message);
        if(message.op==='guide.open'){window.guideMessage=message;return;}
        let result=null;
        if(message.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
        if(message.op==='notice.state')result={accepted:false};
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.reload();page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#notice-guide-link').click();page.wait_for_function('window.guideMessage')
    assert not guide.is_visible(),'Desktop opened an embedded guide'
    page.evaluate('safeCodeNativeResponse({id:guideMessage.id,result:false})')
    page.locator('#accept').click();page.locator('#ip').fill('127.0.0.1');page.locator('#connect').click()
    page.wait_for_selector('#developer-preview')
    assert page.locator('#workspace-screen').is_visible()
    assert not page.locator('#header-guide-link').count()
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
    # 独立窗口页面复用相同指南，小窗口也始终显示底部按钮。
    page.add_init_script('window.chrome={webview:{postMessage(message){window.guideResult=message.confirmed;}}}')
    page.set_viewport_size({'width':400,'height':300});page.goto('https://safe-code.local/guide.html?connecting=1')
    page.wait_for_selector('#guide-done-btn');button=page.locator('#guide-done-btn');bounds=button.bounding_box();assert bounds['y']+bounds['height']<=300
    button.click();assert page.evaluate('window.guideResult') is True
    page.locator('#guide-cancel-btn').click();assert page.evaluate('window.guideResult') is False
    page.screenshot(path=str(output/'guide-small-window.png'))
    assert not errors,errors
    browser.close()
print('GUIDE_CLOSE_NATIVE_BRIDGE_PREVIEW_WITHOUT_SERVER_AND_SMALL_WINDOW_PASSED')
