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
  window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.hostCalls=[];window.keyCalls=[];
  window.chrome={webview:{postMessage(message){
   if(message.op.startsWith('desktopKey.')){
    keyCalls.push(message.op);
    const result=message.op==='desktopKey.enable'?{requiresDevice:true}:message.op==='desktopKey.authorizeDevice'
      ?{configured:true,running:true}:{configured:false,running:false};
    queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));return;
   }
   if(message.op==='host.enable'||message.op==='host.disable'){hostCalls.push(message);return;}
   const result=message.op==='host.status'?{enabled:false,state:'disabled',alias:'win-test',command:'',message:'未啟用'}:
    message.op==='connect'?{server:{statuses:{account:true},osVersion:'24.04',assignedIps:[],observedEgressIp:''}}:
    message.op==='services.preferences'?{enabled:false,serviceAvailable:false}:message.op==='notice.state'?{accepted:false}:null;
   queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
  }}};
 ''')
 page.goto('https://safe-code.local/web/index.html');page.wait_for_function('window.safeCodeNativeReady')
 page.evaluate("showScreen('login')")
 for accept in (False, True):
  page.locator('#desktop-key-login').click()
  page.locator('#desktop-key-input').fill('sc2.fixture')
  page.locator('#desktop-key-enable').click()
  page.locator('#host-dialog').wait_for()
  assert page.locator('#host-alias').is_disabled()
  assert page.locator('#host-enable').is_disabled()
  assert page.evaluate("!keyCalls.includes('desktopKey.authorizeDevice')")
  if accept:
   page.locator('#host-risk').check()
   page.locator('#host-enable').click()
   page.locator('#desktop-key-open').wait_for()
   assert page.evaluate("keyCalls.at(-1)==='desktopKey.authorizeDevice'")
   page.locator('#desktop-key-close').click()
  else:
   page.locator('#host-close').click()
   page.locator('#desktop-key-dialog').wait_for(state='hidden')
   assert page.evaluate("keyCalls.at(-1)==='desktopKey.cancelDevice'")
 page.evaluate("showScreen('login');document.querySelector('#ip').value='192.0.2.42';document.querySelector('#username').value='fixture';document.querySelector('#login-form').onsubmit({preventDefault(){}})")
 page.wait_for_function("!document.querySelector('#host-manage').disabled")
 page.evaluate("switchMode('maintenance');document.querySelector('#host-manage').onclick()")
 page.locator('#host-dialog').wait_for()
 assert page.locator('#host-dialog').is_visible();assert not page.locator('#host-risk').is_checked();assert page.locator('#host-enable').is_disabled()
 assert '刪除' in page.locator('.host-permission-note').inner_text();assert '管理員' in page.locator('.host-permission-note').inner_text()
 page.evaluate("document.querySelector('#host-form').onsubmit({preventDefault(){}})");assert page.evaluate('hostCalls.length')==0
 page.locator('#host-risk').check();page.locator('#host-alias').fill('win');page.locator('#host-enable').click();page.wait_for_function('hostCalls.length===1')
 assert page.evaluate('hostCalls[0].args.riskAccepted') is True;assert page.locator('#host-close').is_disabled();page.keyboard.press('Escape');assert page.locator('#host-dialog').is_visible()
 page.evaluate("window.safeCodeNativeResponse({id:hostCalls[0].id,result:{enabled:true,state:'connected',alias:'win',command:'ssh -F /home/fixture/.ssh/safe-code-host/test/config win',message:'已驗證'}})")
 page.wait_for_function("!document.querySelector('#host-disable').disabled")
 assert page.locator('#host-enable').is_hidden();assert page.locator('#host-copy').is_enabled();assert page.locator('#host-command').input_value().startswith('ssh -F ')
 command_box=page.locator('#host-command').bounding_box();copy_box=page.locator('#host-copy').bounding_box();disable_box=page.locator('#host-disable').bounding_box()
 assert abs(command_box['y']-copy_box['y'])<=1 and abs(command_box['height']-copy_box['height'])<=1
 assert min(copy_box['height'],disable_box['height'])>=36
 page.locator('#host-disable').click();page.wait_for_function('hostCalls.length===2');page.evaluate("window.safeCodeNativeResponse({id:hostCalls[1].id,result:{enabled:false,state:'disabled',alias:'win',command:'',message:'已停用'}})")
 page.wait_for_function("!document.querySelector('#host-risk').checked");assert page.locator('#host-command-area').is_hidden();assert page.locator('#host-enable').is_disabled()
 page.locator('#host-close').click();page.set_viewport_size({'width':740,'height':634});page.locator('#host-manage').scroll_into_view_if_needed()
 bounds=page.locator('#host-manage').bounding_box();assert bounds and bounds['y']>=0 and bounds['y']+bounds['height']<=634
 page.evaluate("document.querySelector('#host-manage').onclick()");page.locator('#host-risk').scroll_into_view_if_needed()
 assert not errors,errors
 (root/'.local/evidence').mkdir(parents=True,exist_ok=True);page.screenshot(path=str(root/'.local/evidence/host-control-permission.png'));browser.close()
print('HOST_PERMISSION_OPT_IN_BUSY_COMMAND_AND_REVOCATION_UI_OK')
