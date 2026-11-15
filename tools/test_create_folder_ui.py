"""检查创建空目录、自动选中、重复提交保护和守护状态，使用本地界面夹具。"""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':740,'height':634});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        url=r.request.url
        if not url.startswith('https://safe-code.local/'):r.abort();return
        f=root/url.split('safe-code.local/',1)[1].split('?',1)[0]
        r.fulfill(path=str(f)) if f.is_file() else r.fulfill(status=404,body='')
    page.route('**/*',route)
    page.add_init_script('''
    window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.createCalls=[];
    window.chrome={webview:{postMessage(message){
      if(message.op==='folder.create'){window.createCalls.push(message);return;}
      let result=message.op==='services.preferences'?{enabled:false,serviceAvailable:false}:message.op==='notice.state'?{accepted:false}:message.op==='mount.settings'?[{local:'C:\\\\Projects',remote:''}]:message.op==='folder.list'?{path:'/home/fixture/workspace',parent:'/home/fixture',directories:[]}:null;
      queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result}));
    }}};
    ''')
    page.goto('https://safe-code.local/index.html');page.wait_for_function('window.safeCodeNativeReady')
    page.evaluate("showScreen('workspace')");page.evaluate("document.querySelector('#mount-settings').onclick()")
    page.locator('[data-path=remote]').locator('..').get_by_role('button',name='选择目录').click()
    page.wait_for_function("!document.querySelector('#folder-confirm').disabled")
    assert page.locator('#folder-confirm').is_enabled(),'空目录不可选'
    page.locator('#folder-new').click();page.locator('#create-folder-name').fill('Project-2026 中文');page.locator('#create-folder-form [type=submit]').click()
    page.wait_for_function('window.createCalls.length===1')
    assert page.locator('#create-folder-form [type=submit]').is_disabled()
    assert page.locator('#create-folder-dialog .close').is_disabled()
    page.keyboard.press('Escape');assert page.locator('#create-folder-dialog').is_visible()
    page.evaluate("document.querySelector('#create-folder-form').onsubmit({preventDefault(){}})");assert page.evaluate('window.createCalls.length')==1
    assert page.evaluate('window.createCalls[0].args.name')=='Project-2026 中文'
    page.evaluate("window.safeCodeNativeResponse({id:window.createCalls[0].id,result:{path:'/home/fixture/workspace/Project-2026 中文'}})")
    page.wait_for_function("!document.querySelector('#remote-folder-dialog').open")
    assert page.locator('[data-path=remote]').input_value()=='/home/fixture/workspace/Project-2026 中文'
    page.locator('#close-mount').click()
    page.evaluate("window.safeCodeMountStatus({count:2,state:'reconnecting',message:'保留缓存并重连'})")
    assert '重连中' in page.locator('#mount-guard-status').inner_text()
    assert page.locator('#mount-guard-status').evaluate('(el)=>el.getBoundingClientRect().right<=innerWidth')
    assert not errors,errors
    (root/'.local/evidence').mkdir(exist_ok=True);page.screenshot(path=str(root/'.local/evidence/mount-guard-status.png'))
    browser.close()
print('CREATE_EMPTY_FOLDER_AUTOSELECT_DUPLICATE_GUARD_AND_MOUNT_STATUS_PASSED')
