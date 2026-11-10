#!/usr/bin/env python3
"""验证更新提示的状态、可见性、点击和窄窗口布局；不调用线上更新接口。"""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':360,'height':536});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(request):
        url=request.request.url
        if not url.startswith('https://safe-code.local/'):request.abort();return
        file=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        if file.is_file():request.fulfill(path=str(file))
        else:request.fulfill(status=404,body='')
    page.route('**/*',route)
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;
      window.updateFixture={currentVersion:'Beta-0.4.3.3',version:'Beta-0.4.3.4',status:'发现新版本',canInstall:false,page:null};
      window.chrome={webview:{postMessage(message){
        let result=null;
        if(message.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
        if(message.op==='notice.state')result={accepted:false};
        if(message.op==='updates.check'){
          if(window.failUpdate){queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,error:'暂时离线'}));return;}
          result=window.updateFixture;
        }
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady')
    notice=page.locator('#title-update-notice');assert not notice.is_visible()
    page.locator('[data-open-services]:visible').first.click();page.locator('#check-update').click()
    page.wait_for_function("!document.querySelector('#title-update-notice').hidden")
    page.locator('#services-dialog .close').click()
    bounds=notice.bounding_box();controls=page.locator('.window-controls').bounding_box()
    assert bounds['x']+bounds['width']<=controls['x'], 'Update badge overlaps window controls'
    assert 'has-update' in page.locator('#services-entry').get_attribute('class')
    output=root/'.local/evidence';output.mkdir(exist_ok=True,parents=True)
    page.screenshot(path=str(output/'update-notice-narrow.png'))
    notice.click();assert page.locator('#services-dialog').is_visible()
    page.evaluate('window.failUpdate=true');page.locator('#check-update').click()
    page.wait_for_function("document.querySelector('#update-status').textContent.includes('暂时离线')")
    assert page.locator('#title-update-notice').evaluate('element=>!element.hidden'), 'Known update disappeared on network failure'
    page.evaluate("window.failUpdate=false;window.updateFixture={currentVersion:'Beta-0.4.3.3',version:null,status:'已是最新版本',canInstall:false,page:null}")
    page.locator('#check-update').click();page.wait_for_function("document.querySelector('#title-update-notice').hidden")
    assert page.locator('#services-entry').inner_text()=='更新与反馈'
    assert 'has-update' not in page.locator('#services-entry').get_attribute('class')
    page.locator('#services-dialog .close').click()
    assert not errors,errors
    browser.close()
print('UPDATE_NOTICE_VISIBILITY_CLICK_NARROW_LAYOUT_NETWORK_FAILURE_AND_CLEAR_PASSED')
