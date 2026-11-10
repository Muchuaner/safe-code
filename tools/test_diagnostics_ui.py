#!/usr/bin/env python3
"""Check the diagnostic consent UI without network or a real VPS."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':360,'height':536});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def route(request):
        url=request.request.url
        if not url.startswith('https://safe-code.local/'):
            request.abort();return
        file=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        if file.is_file():request.fulfill(path=str(file))
        else:request.fulfill(status=404,body='')
    page.route('**/*',route)
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;
      window.fixturePrefs={enabled:false,confirmed:true,diagnosticsEnabled:true,serviceAvailable:true,recipient:'collector.invalid'};
      window.diagnosticConsentCalls=[];
      window.chrome={webview:{postMessage(message){
        let result=null;
        if(message.op==='services.preferences')result=window.fixturePrefs;
        if(message.op==='diagnostics.consent'){window.diagnosticConsentCalls.push(message.args.enabled);window.fixturePrefs.diagnosticsEnabled=message.args.enabled;result=window.fixturePrefs;}
        if(message.op==='notice.state')result={accepted:false};
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady')
    page.wait_for_function("!document.querySelector('#settings-diagnostics-consent').disabled")
    page.wait_for_function("document.querySelector('#notice-screen').hidden===false")
    assert page.locator('#diagnostics-consent').is_checked()
    assert page.locator('#accept').bounding_box()['y']+page.locator('#accept').bounding_box()['height']<=536, 'Notice confirmation is inaccessible'
    output=root/'.local/evidence';output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/'diagnostics-notice.png'))
    page.locator('[data-open-services]:visible').first.click()
    toggle=page.locator('#settings-diagnostics-consent')
    assert toggle.is_checked()
    toggle.uncheck();page.wait_for_function('window.diagnosticConsentCalls.length===1')
    assert not page.locator('#settings-usage-consent').is_checked(), 'Diagnostic opt-in changed usage consent'
    toggle.check();page.wait_for_function('window.diagnosticConsentCalls.length===2')
    assert page.evaluate('window.diagnosticConsentCalls')==[False,True]
    assert page.locator('#diagnostics-consent').is_checked()
    assert '不发送原始日志' in page.locator('#services-dialog').inner_text()
    page.locator('#feedback-send').scroll_into_view_if_needed()
    bounds=page.locator('#feedback-send').bounding_box()
    assert bounds and 0<=bounds['y'] and bounds['y']+bounds['height']<=536, 'Feedback became inaccessible in small window'
    toggle.scroll_into_view_if_needed()
    output=root/'.local/evidence';output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/'diagnostics-settings.png'))
    assert not errors,errors
    browser.close()
print('DIAGNOSTIC_DEFAULT_CHECKED_INDEPENDENT_CONSENT_AND_SMALL_WINDOW_PASSED')
