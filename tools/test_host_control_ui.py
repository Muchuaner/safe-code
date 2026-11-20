"""Permission gate and command delivery against the real desktop bridge UI."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page(viewport={'width':1000,'height':900});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
 def route(route):
  path=root/route.request.url.split('safe-code.local/',1)[-1].split('?',1)[0]
  route.fulfill(path=str(path)) if path.is_file() else route.fulfill(status=404,body='')
 page.route('**/*',route)
 page.add_init_script('''
  window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.hostCalls=[];
  window.chrome={webview:{postMessage(message){
   if(message.op==='host.enable'||message.op==='host.disable'){hostCalls.push(message);return;}
   const result=message.op==='host.status'?{enabled:false,state:'disabled',alias:'win-test',command:'',message:'未启用'}:
    message.op==='services.preferences'?{enabled:false,serviceAvailable:false}:message.op==='notice.state'?{accepted:false}:null;
   queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
  }}};
 ''')
 page.goto('https://safe-code.local/web/index.html');page.wait_for_function('window.safeCodeNativeReady')
 page.evaluate("showScreen('workspace');switchMode('maintenance');document.querySelector('#host-manage').onclick()")
 assert page.locator('#host-dialog').is_visible();assert not page.locator('#host-risk').is_checked();assert page.locator('#host-enable').is_disabled()
 assert '删除' in page.locator('.host-permission-note').inner_text();assert '管理员' in page.locator('.host-permission-note').inner_text()
 page.evaluate("document.querySelector('#host-form').onsubmit({preventDefault(){}})");assert page.evaluate('hostCalls.length')==0
 page.locator('#host-risk').check();page.locator('#host-alias').fill('win');page.locator('#host-enable').click();page.wait_for_function('hostCalls.length===1')
 assert page.evaluate('hostCalls[0].args.riskAccepted') is True;assert page.locator('#host-close').is_disabled();page.keyboard.press('Escape');assert page.locator('#host-dialog').is_visible()
 page.evaluate("window.safeCodeNativeResponse({id:hostCalls[0].id,result:{enabled:true,state:'connected',alias:'win',command:'ssh -F /home/fixture/.ssh/safe-code-host/test/config win',message:'已验证'}})")
 page.wait_for_function("!document.querySelector('#host-disable').disabled")
 assert page.locator('#host-enable').is_hidden();assert page.locator('#host-copy').is_enabled();assert page.locator('#host-command').input_value().startswith('ssh -F ')
 page.locator('#host-disable').click();page.wait_for_function('hostCalls.length===2');page.evaluate("window.safeCodeNativeResponse({id:hostCalls[1].id,result:{enabled:false,state:'disabled',alias:'win',command:'',message:'已停用'}})")
 page.wait_for_function("!document.querySelector('#host-risk').checked");assert page.locator('#host-command-area').is_hidden();assert page.locator('#host-enable').is_disabled()
 page.locator('#host-close').click();page.set_viewport_size({'width':740,'height':634});page.locator('#host-manage').scroll_into_view_if_needed()
 bounds=page.locator('#host-manage').bounding_box();assert bounds and bounds['y']>=0 and bounds['y']+bounds['height']<=634
 page.evaluate("document.querySelector('#host-manage').onclick()");page.locator('#host-risk').scroll_into_view_if_needed()
 assert not errors,errors
 (root/'.local/evidence').mkdir(exist_ok=True);page.screenshot(path=str(root/'.local/evidence/host-control-permission.png'));browser.close()
print('HOST_PERMISSION_OPT_IN_BUSY_COMMAND_AND_REVOCATION_UI_OK')
