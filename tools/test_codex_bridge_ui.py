"""Integration UI contract; supplied EXE is not executed by this test."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=browser.new_page(viewport={'width':1000,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  f=root/r.request.url.split('safe-code.local/',1)[-1].split('?',1)[0];r.fulfill(path=str(f)) if f.is_file() else r.fulfill(status=404,body='')
 page.route('**/*',route);page.add_init_script('''
 window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.bridgeCalls=[];
 window.chrome={webview:{postMessage(m){
  if(['bridge.start','bridge.stop','bridge.copy-key','bridge.copy-config'].includes(m.op)){bridgeCalls.push(m);if(m.op.startsWith('bridge.copy-'))queueMicrotask(()=>safeCodeNativeResponse({id:m.id,result:true}));return;}
  const result=m.op==='bridge.status'?{running:false,connected:false,cleanupPending:false,port:18797,model:'sonnet',writable:false,message:'未启动'}:m.op==='services.preferences'?{enabled:false,serviceAvailable:false}:m.op==='notice.state'?{accepted:false}:null;
  queueMicrotask(()=>safeCodeNativeResponse({id:m.id,result}));
 }}};
 ''')
 page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady');page.evaluate("showScreen('workspace');switchMode('maintenance');void document.querySelector('#bridge-manage').onclick()")
 assert '亚撒西の空白' in page.locator('#bridge-credit').inner_text()
 assert page.locator('.unified-maintenance-cards > :last-child').get_attribute('class')=='advanced-card bridge-card'
 assert '未经本人测试' not in page.locator('.bridge-card').inner_text()
 assert page.locator('#codex-launch').is_disabled()
 page.wait_for_selector('#app-message-dialog[open]')
 assert '自行承担后果' in page.locator('#app-message-body').inner_text()
 page.locator('#app-message-cancel').click()
 assert page.locator('#bridge-dialog').is_hidden()
 page.evaluate("void document.querySelector('#bridge-manage').onclick()")
 page.locator('#app-message-confirm').click()
 page.wait_for_selector('#bridge-dialog[open]')
 assert not page.locator('#bridge-consent').is_checked();assert not page.locator('#bridge-write').is_checked();assert page.locator('#bridge-start').is_disabled()
 page.evaluate("document.querySelector('#bridge-form').onsubmit({preventDefault(){}})");assert page.evaluate('bridgeCalls.length')==0
 page.locator('#bridge-consent').check();page.locator('#bridge-start').click();page.wait_for_function('bridgeCalls.length===1');assert page.evaluate('bridgeCalls[0].args.writable') is False;assert page.evaluate('bridgeCalls[0].args.thirdPartyAccepted') is True
 assert page.locator('#bridge-close').is_disabled();page.keyboard.press('Escape');assert page.locator('#bridge-dialog').is_visible()
 page.evaluate("safeCodeNativeResponse({id:bridgeCalls[0].id,result:{running:true,connected:true,cleanupPending:false,port:18797,model:'sonnet',writable:false,message:'已连接',configuration:'model_provider = \\\"safe_code_claude\\\"'}})")
 page.wait_for_function("!document.querySelector('#bridge-stop').disabled");assert page.locator('#bridge-copy-key').is_enabled();assert page.locator('#codex-launch').is_enabled();page.locator('#bridge-copy-key').click();assert page.evaluate("bridgeCalls[1].op==='bridge.copy-key' && !('text' in bridgeCalls[1].args)")
 page.locator('#bridge-stop').click();page.wait_for_function('bridgeCalls.length===3');page.evaluate("safeCodeNativeResponse({id:bridgeCalls[2].id,result:{running:false,connected:false,cleanupPending:false,port:18797,model:'sonnet',writable:false,message:'已停止'}})")
 page.wait_for_function("!document.querySelector('#bridge-consent').checked");assert page.locator('#bridge-config-area').is_hidden();assert page.locator('#codex-launch').is_disabled();assert page.locator('#bridge-start').is_disabled()
 page.locator('#bridge-close').click();page.set_viewport_size({'width':740,'height':634});page.locator('#bridge-manage').scroll_into_view_if_needed();box=page.locator('#bridge-manage').bounding_box();assert box and 0<=box['y']<634
 page.evaluate("void document.querySelector('#bridge-manage').onclick()");page.locator("#app-message-confirm").click();page.locator('#bridge-consent').scroll_into_view_if_needed();assert not errors,errors
 (root/'.local/evidence').mkdir(exist_ok=True);page.screenshot(path=str(root/'.local/evidence/codex-bridge-settings.png'));browser.close()
print('CODEX_BRIDGE_CONSENT_READONLY_BUSY_SECRET_COPY_AND_STOP_UI_OK')
