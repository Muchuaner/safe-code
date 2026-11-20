"""Startup restores only saved opt-in; failed/restoring gateways never receive a green badge."""
from pathlib import Path
from playwright.sync_api import sync_playwright
import json
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    for remembered,notice,failure,warning in [(False,True,False,False),(True,True,False,False),(True,False,False,False),(True,True,True,False),(True,True,False,True)]:
        context=browser.new_context(viewport={'width':740,'height':634});page=context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def route(r):
            f=root/r.request.url.split('safe-code.local/',1)[-1].split('?',1)[0]
            r.fulfill(path=str(f)) if f.is_file() else r.fulfill(status=404,body='')
        page.route('**/*',route)
        page.add_init_script('window.fixture='+json.dumps(dict(remembered=remembered,notice=notice,failure=failure,warning=warning))+';'+'''
        window.__safeCodeDesktop=true;window.calls=[];
        const saved={host:'192.0.2.42',username:'root',password:'fixture',port:22};
        window.chrome={webview:{postMessage(m){
          calls.push(m.op);let result=null,error=null;
          if(m.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
          if(m.op==='notice.state')result={accepted:fixture.notice};
          if(m.op==='login.load')result=saved;
          if(m.op==='desktopKey.status')result={configured:false,running:false};
          if(m.op==='gateways.resume-settings')result=fixture.remembered?saved:null;
          if(m.op==='connect'){
            if(fixture.failure)error='fixture SSH unavailable';
            else result={server:{osVersion:'24.04',statuses:{account:true},assignedIps:[],observedEgressIp:''},gatewayWarnings:fixture.warning?['Claude 网关恢复失败：fixture']:[]};
          }
          if(m.op==='claudeDesktop.status')result={active:!fixture.warning,managed:true,installed:true,state:fixture.warning?'attention':'active'};
          if(m.op==='bridge.status')result={running:!fixture.warning,connected:!fixture.warning,configuration:fixture.warning?'':'fixture'};
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result,error}));
        }}};
        ''')
        page.goto('https://safe-code.local/web/index.html');page.wait_for_function('window.safeCodeNativeReady')
        if remembered and notice:
            page.wait_for_function("calls.includes('connect')")
            if failure:
                page.wait_for_selector('#app-message-dialog[open]')
                assert 'SSH unavailable' in page.locator('#app-message-dialog').inner_text()
            else:
                page.wait_for_function("calls.includes('claudeDesktop.status')&&calls.includes('bridge.status')")
                if warning:page.wait_for_selector('#app-message-dialog[open]')
            enabled=not failure and not warning
            assert page.locator('#claude-gateway-status').inner_text()=='Claude网关'+('已启用' if enabled else '未启用')
            assert page.locator('#codex-gateway-status').inner_text()=='Codex网关'+('已启用' if enabled else '未启用')
        else:
            page.wait_for_timeout(120)
            assert 'connect' not in page.evaluate('calls')
            assert page.locator('#claude-gateway-status').inner_text()=='Claude网关未启用'
        assert not set(page.evaluate('calls')) & {'claudeDesktop.start','bridge.start'},'frontend must never forge fresh consent for restore'
        assert not errors,errors
        context.close()
    browser.close()
print('GATEWAY_STARTUP_OPT_IN_NOTICE_FAILURE_AND_LIVE_BADGES_OK')
