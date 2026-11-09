#!/usr/bin/env python3
"""Headless native-bridge UI fixture; never connects to a VPS or the installed app."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':1000,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def route(request):
        url=request.request.url
        if not url.startswith('https://safe-code.local/'):
            request.abort();return
        file=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        if file.is_file():request.fulfill(path=str(file))
        else:request.fulfill(status=404,body='')
    page.route('**/*',route)
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.mountCalls=0;
      window.chrome={webview:{postMessage(message){
        if(message.op==='mount.apply'){window.mountCalls++;window.mountRequest=message;return;}
        const result=message.op==='services.preferences'?{enabled:false,serviceAvailable:false}:
          message.op==='notice.state'?{accepted:false}:
          message.op==='mount.settings'?[{local:'C:\\\\Projects',remote:'/home/fixture/shared'}]:null;
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady')
    page.evaluate("showScreen('workspace')");page.evaluate("document.querySelector('#mount-settings').onclick()")
    save=page.locator('#mount-form button[type=submit]');save.click()
    page.wait_for_function("document.querySelector('#mount-form').getAttribute('aria-busy')==='true'")
    assert save.inner_text()=='正在挂载…' and save.is_disabled()
    assert page.locator('#close-mount').is_disabled()
    assert page.locator('#mount-dialog .connection-spinner').is_visible()
    page.keyboard.press('Escape');assert page.locator('#mount-dialog').is_visible()
    page.evaluate("document.querySelector('#mount-form').onsubmit({preventDefault(){}})")
    assert page.evaluate('window.mountCalls')==1
    page.evaluate("window.safeCodeNativeLog('mount_apply','正在检查服务器目录…')")
    page.wait_for_function("document.querySelector('#mount-dialog [role=status]').textContent.includes('已用时 1 秒')")
    assert '正在检查服务器目录' in page.locator('#mount-dialog [role=status]').inner_text()
    output=root/'.local/evidence';output.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(output/'mount-fix-progress.png'))
    page.evaluate("window.safeCodeNativeResponse({id:window.mountRequest.id,result:{paths:['/home/fixture/shared']}})")
    page.wait_for_function("!document.querySelector('#mount-dialog').open")
    assert page.locator('#mount-summary').inner_text()=='已挂载 1 组目录。'
    page.evaluate("document.querySelector('#mount-settings').onclick()");save.click()
    page.evaluate("window.safeCodeNativeResponse({id:window.mountRequest.id,error:'服务器目录不是空目录\\n请选择空目录后重试。'})")
    page.wait_for_function("document.querySelector('#app-message-dialog').open")
    assert '请选择空目录后重试。' in page.locator('#app-message-body').inner_text()
    assert not save.is_disabled() and save.inner_text()=='保存设置'
    assert page.locator('#mount-dialog').is_visible()
    page.locator('#app-message-confirm').click()
    page.locator('#mount-list .mount-remove').click();save.click()
    page.evaluate("window.safeCodeNativeResponse({id:window.mountRequest.id,result:{paths:[]}})")
    page.wait_for_function("!document.querySelector('#mount-dialog').open")
    assert page.locator('#mount-summary').inner_text()=='已卸载所有目录。'
    assert not errors,errors
    print('MOUNT_BUSY_SPINNER_ELAPSED_LOG_DUPLICATE_ESCAPE_SUCCESS_ERROR_RETRY_AND_CLEAR_PASSED')
    browser.close()
