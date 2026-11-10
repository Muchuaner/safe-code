#!/usr/bin/env python3
"""出入口设置的真实前端等待、重复提交、确认结果和保存回读检查；不连接 VPS。"""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':740,'height':634});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    def route(request):
        url=request.request.url
        if not url.startswith('https://safe-code.local/'):request.abort();return
        file=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        if file.is_file():request.fulfill(path=str(file))
        else:request.fulfill(status=404,body='')
    page.route('**/*',route)
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.ipCalls=[];window.allCalls=[];window.remembered='8.8.8.8';
      window.serverFixture={osId:'ubuntu',osVersion:'24.04',assignedIps:['8.8.8.8','1.1.1.1'],observedEgressIp:'1.1.1.1',ipPolicyHealthy:true,statuses:{},ipPolicy:{ingressIp:'8.8.8.8',egressIp:'1.1.1.1',lockIngress:true,lockEgress:true}};
      window.finishIp=()=>{const message=window.ipCalls.at(-1);window.serverFixture.ipPolicy=message.args;window.remembered=message.args.ingressIp;window.safeCodeNativeResponse({id:message.id,result:window.serverFixture});};
      window.chrome={webview:{postMessage(message){
        window.allCalls.push(message.op);let result=null;
        if(message.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
        if(message.op==='notice.state')result={accepted:false};
        if(message.op==='connect')result={ready:true,server:window.serverFixture};
        if(message.op==='inspect')result=window.serverFixture;
        if(message.op==='login.load')result={host:window.remembered};
        if(message.op==='metrics')result={latencyMs:100,lossPercent:0};
        if(message.op==='ip.info')result={location:'fixture',score:100,regionCode:'kr'};
        if(message.op==='ip.apply'){
          if(window.failNextIp){window.failNextIp=false;queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,error:'出口验证失败，已回退'}));return;}
          if(message.args.egressIp==='192.0.2.123'){queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,error:'未分配地址'}));return;}
          window.ipCalls.push(message);return;
        }
        if(message.op==='acceptance.done')window.ipResult=message.args;
        queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
      }}};
    ''')
    page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady')
    page.evaluate("void window.safeCodeRunIpAcceptance({host:'8.8.8.8',ingressIp:'8.8.8.8',egressIp:'1.1.1.1',username:'fixture',password:'fixture-only'})")
    try:page.wait_for_function('window.ipCalls.length===1',timeout=8000)
    except Exception:
        print(page.evaluate('({result:window.ipResult,calls:window.allCalls,errors:document.querySelector("#app-message-body").textContent})'));print(errors);raise
    assert page.locator('#ip-policy-form').get_attribute('aria-busy')=='true'
    assert page.locator('#close-ip-policy').is_disabled()
    assert page.locator('#ip-policy-form .connection-spinner').is_visible()
    page.evaluate("document.querySelector('#ip-policy-form').onsubmit({preventDefault(){}})")
    assert page.evaluate('window.ipCalls.length')==1
    page.keyboard.press('Escape');assert page.locator('#ip-policy-dialog').is_visible()
    page.evaluate("window.safeCodeNativeLog('ip_policy','正在用全新连接验证入口…')")
    assert '全新连接' in page.locator('#ip-policy-form [role=status]').inner_text()
    output=root/'.local/evidence';output.mkdir(exist_ok=True,parents=True)
    page.screenshot(path=str(output/'ip-policy-progress.png'))
    page.evaluate('window.finishIp()');page.wait_for_function('window.ipCalls.length===2')
    page.evaluate('window.finishIp()');page.wait_for_function('window.ipResult')
    assert page.evaluate('window.ipResult.success'),page.evaluate('window.ipResult')
    assert page.locator('#server-address').inner_text()=='1.1.1.1'
    page.locator('#change-ips').click()
    assert page.locator('#ip-policy-form input[type=text]').count()==0
    assert page.locator('#egress-ip-input').evaluate('element=>element.tagName')=='SELECT'
    page.locator('#egress-ip-trigger').click()
    assert page.locator('#egress-ip-options [role=option]').count()==2
    assert page.locator('#egress-ip-options [aria-selected=true]').inner_text()=='1.1.1.1'
    page.screenshot(path=str(output/'ip-picker-menu.png'))
    page.keyboard.press('ArrowUp');page.keyboard.press('Enter')
    assert page.locator('#egress-ip-input').input_value()=='8.8.8.8'
    page.locator('#egress-ip-trigger').click();page.keyboard.press('Escape')
    assert page.locator('#ip-policy-dialog').is_visible()
    assert not page.locator('#egress-ip-options').is_visible()
    page.locator('#egress-ip-input').evaluate("element=>element.value='192.0.2.123'")
    page.evaluate("document.querySelector('#ip-policy-form').onsubmit({preventDefault(){}})")
    assert page.evaluate('window.ipCalls.length')==2, 'Unassigned IP reached the native bridge'
    page.locator('#egress-ip-trigger').click()
    page.locator('#egress-ip-options [data-ip=\"1.1.1.1\"]').click()
    page.evaluate('window.failNextIp=true')
    page.locator('#ip-policy-form [type=submit]').click()
    page.wait_for_function("document.querySelector('#app-message-dialog').open")
    assert page.locator('#egress-ip-input').input_value()=='1.1.1.1'
    assert not page.locator('#close-ip-policy').is_disabled()
    assert page.locator('#ip-policy-dialog').is_visible()
    page.locator('#app-message-dialog [data-dialog-dismiss]').click()
    page.locator('#ip-policy-form [type=submit]').click()
    page.wait_for_function('window.ipCalls.length===3')
    page.evaluate('window.finishIp()')
    page.wait_for_function("!document.querySelector('#ip-policy-dialog').open")
    set_empty=page.evaluate('setDetectedIpOptions([])')
    assert page.locator('#egress-ip-trigger').is_disabled()
    assert page.locator('#ip-policy-form [type=submit]').is_disabled()
    assert not errors,errors
    browser.close()
print('IP_BUSY_LOG_DUPLICATE_ESCAPE_SWITCH_RESTORE_READBACK_VALIDATION_FAILURE_AND_RETRY_PASSED')
