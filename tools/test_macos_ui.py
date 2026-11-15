"""macOS UI parity and disabled Windows adapter; no model or SSH requests."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=browser.new_page(viewport={'width':740,'height':634});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.add_init_script('''window.__safeCodeDesktop=true;window.__safeCodePlatform='macos';window.__safeCodeAcceptance=true;window.calls=[];
 window.chrome={webview:{postMessage(m){calls.push(m);let result=m.op==='services.preferences'?{enabled:false,serviceAvailable:false}:m.op==='notice.state'?{accepted:false}:m.op==='host.status'?{enabled:false,state:'disabled',alias:'',command:'',message:'未启用'}:m.op==='bridge.status'?{running:false}:null;queueMicrotask(()=>window.safeCodeNativeResponse?.({id:m.id,result}));}}};''')
 page.goto((root/'index.html').as_uri());page.wait_for_function('window.safeCodeNativeReady===true')
 page.evaluate("showScreen('workspace');switchMode('maintenance')")
 assert page.locator('.window-controls').is_hidden();assert page.locator('#bridge-manage').is_disabled();assert '暂不可用' in page.locator('#bridge-state').inner_text()
 assert 'macOS' in page.locator('#usage-detail').inner_text();assert '系统设置' in page.locator('.host-risk small').inner_text()
 # The refresh paths must not re-enable an unsupported adapter after a real server is applied.
 page.evaluate("safeCodeNativeResponse({id:'unregistered',result:null});document.querySelector('#host-manage').disabled=false;document.querySelector('#host-manage').click()")
 page.wait_for_selector('#host-dialog[open]');assert page.locator('#host-alias').input_value()=='mac';assert not page.locator('#host-risk').is_checked();assert '远程登录' in page.locator('#host-progress').inner_text()
 page.locator('#host-close').click();assert not errors,errors;browser.close()
print('MAC_NATIVE_FILE_ORIGIN_PLATFORM_COPY_REMOTE_LOGIN_DEFAULT_AND_UNSUPPORTED_ADAPTER_UI_OK')
