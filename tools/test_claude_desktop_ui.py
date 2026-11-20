"""Claude Desktop 接入卡片/弹窗夹具；不读写真实注册表，不发模型请求。"""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
secret='sk-fixture-never-in-dom-9f3c'
base={"active":False,"managed":False,"configured":True,"installed":True,"state":"configured",
      "message":"已识别现有官方网关配置，可接管而无需重新填写密钥",
      "endpoint":"https://gateway.example/anthropic","model":"claude-sonnet-4-5-20250929","display":"Opus",
      "credentialPresent":True,"takeover":False,"startedAt":None,"stoppedAt":None,
      "usage":{"available":True,"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0,"note":None},
      "usageSource":"Claude Desktop 官方本地会话"}
active={**base,"active":True,"managed":True,"state":"active","takeover":True,"startedAt":"2026-11-17T01:00:00Z",
        "usage":{"available":True,"input":100,"output":10,"cacheRead":900,"cacheWrite":50,"total":1060,"note":None}}
base.update(model='claude-opus-5-5',endpoint='',state='unconfigured')
active.update(model='claude-opus-5-5',endpoint='http://127.0.0.1:18899',takeover=False,
              message='SSH 网关已连接，可在 Claude Desktop 中开始对话')
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':1000,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.claudeStartCalls=[];window.claudeStopCalls=0;window.clientOpenCalls=[];window.clientOpenError=null;
      window.claudeWarningAccepts=0;window.keyEnableCalls=[];window.keyDisableCalls=0;
      const hostDisabled={enabled:false,state:'disabled',alias:'mac',command:'',message:'未启用'};
      window.hostState=hostDisabled;window.hostEnableCalls=[];window.hostEnableError=null;
      window.chrome={webview:{postMessage(m){
        if(m.op==='connect'){
          const server={statuses:{account:true},osVersion:'24.04',assignedIps:[],observedEgressIp:''};
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:{server}}));return;
        }
        if(m.op==='host.enable'){
          window.hostEnableCalls.push(m.args);
          if(window.hostEnableError){
            queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,error:window.hostEnableError}));return;
          }
          window.hostState={enabled:true,state:'connected',alias:m.args.alias,command:'ssh fixture',message:'已通过登录验证'};
        }
        if(m.op==='host.status'||m.op==='host.enable'){
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.hostState}));return;
        }
        if(m.op==='notice.claudeDesktop.accept'){
          window.claudeWarningAccepts++;
          localStorage.setItem('claude-desktop-warning-fixture','accepted');
        }
        if(m.op.startsWith('notice.claudeDesktop.')){
          const accepted=localStorage.getItem('claude-desktop-warning-fixture')==='accepted';
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:accepted}));return;
        }
        if(m.op.startsWith('desktopKey.')){
          if(m.op==='desktopKey.status'&&window.keyStatusFailure){
            queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,...window.keyStatusFailure}));return;
          }
          if(m.op==='desktopKey.enable'){
            window.keyEnableCalls.push(m.args);
            if(m.args.useSaved? !window.keyConfigured : !['sc1.fixture','sc1.replacement'].includes(m.args.key)){
              queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,error:'Key 无效或已撤销。'}));return;
            }
            window.keyConfigured=true;window.keyRunning=true;
          }
          if(m.op==='desktopKey.disable'){window.keyDisableCalls++;window.keyConfigured=false;window.keyRunning=false;}
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:{configured:!!window.keyConfigured,running:!!window.keyRunning}}));return;
        }
        if(m.op==='claudeDesktop.start'){window.claudeStartCalls.push(m);queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.__claudeActive}));return;}
        if(m.op==='claudeDesktop.stop'){window.claudeStopCalls++;queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.__claudeStopped}));return;}
        if(m.op==='claudeDesktop.open'||m.op==='codex.open'){window.clientOpenCalls.push(m.op);queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:'已请求打开客户端。',error:window.clientOpenError}));return;}
        if(m.op==='claudeDesktop.status'){queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.__claudeStatus}));return;}
        const result=m.op==='services.preferences'?{enabled:false,serviceAvailable:false}:m.op==='notice.state'?{accepted:false}:null;
        queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result}));
      }}};
    ''')
    def route(r):
        f=root/r.request.url.split('safe-code.local/',1)[-1].split('?',1)[0]
        if f.is_file():r.fulfill(path=str(f))
        else:r.fulfill(status=404,body='')
    page.route('**/*',route)
    page.goto('https://safe-code.local/web/index.html');page.wait_for_function('window.safeCodeNativeReady')
    page.evaluate("showScreen('login')")
    page.evaluate("window.keyStatusFailure={error:'暂时无法读取接入状态'}")
    page.locator('#desktop-key-login').click()
    page.get_by_text('暂时无法读取接入状态').wait_for()
    assert page.locator('#desktop-key-input').is_visible()
    page.locator('#desktop-key-close').click()
    page.evaluate("window.keyStatusFailure=null")
    page.locator('#desktop-key-login').click()
    page.locator('#desktop-key-input').fill('sc1.fixture')
    page.locator('#desktop-key-enable').click()
    assert page.locator('#desktop-key-open').is_visible() and page.locator('#desktop-key-input').input_value()==''
    assert page.locator('#desktop-key-form .config-description br').count()==1
    assert page.locator('#desktop-key-progress').inner_text().startswith('已启用。\n首次接入')
    assert page.locator('#desktop-key-open').inner_text()=='打开\nClaude Desktop'
    page.locator('#desktop-key-close').click()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
    page.evaluate("window.keyRunning=false;window.keyStatusFailure={result:{configured:true,running:false,error:'本机策略无法访问'}}")
    page.locator('#desktop-key-settings').click()
    page.get_by_text('已保存 Key，暂未恢复接入。',exact=False).wait_for()
    assert not page.locator('#desktop-key-input').is_visible()
    assert page.locator('#desktop-key-disable').is_enabled()
    assert page.locator('#desktop-key-replace').is_enabled() and page.locator('#desktop-key-resume').is_enabled()
    assert not page.locator('#desktop-key-open').is_visible()
    page.locator('#desktop-key-resume').click()
    page.locator('#desktop-key-open').wait_for()
    assert page.evaluate('window.keyEnableCalls.at(-1)')=={'useSaved':True}
    page.locator('#desktop-key-close').click()
    page.evaluate("window.keyStatusFailure=null")
    page.locator('#desktop-key-settings').click()
    page.locator('#desktop-key-replace').click()
    assert page.locator('#desktop-key-input').evaluate('e=>document.activeElement===e')
    assert page.locator('#desktop-key-enable').inner_text()=='验证并更换'
    page.locator('#desktop-key-input').fill('sc1.invalid')
    page.locator('#desktop-key-enable').click()
    page.get_by_text('Key 无效或已撤销。',exact=True).wait_for()
    assert page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-input').input_value()==''
    assert page.evaluate('window.keyRunning && window.keyDisableCalls===0')
    page.locator('#desktop-key-replace').click()
    assert not page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-open').is_visible()
    page.locator('#desktop-key-replace').click()
    page.locator('#desktop-key-input').fill('sc1.unsent')
    page.locator('#desktop-key-close').click()
    page.locator('#desktop-key-settings').click()
    assert not page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-input').input_value()==''
    page.locator('#desktop-key-replace').click()
    page.locator('#desktop-key-input').fill('sc1.replacement')
    page.locator('#desktop-key-enable').click()
    page.locator('#desktop-key-open').wait_for()
    assert page.evaluate('window.keyEnableCalls.at(-1)')=={'key':'sc1.replacement','replace':True}
    assert page.evaluate('window.keyDisableCalls===0')
    page.locator('#desktop-key-disable').click()
    assert page.locator('#desktop-key-enable').is_visible()
    page.locator('#desktop-key-close').click()
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:true,configuration:'fixture'})")
    assert page.locator('#codex-gateway-status').inner_text()=='Codex网关已启用'
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:false})")
    assert page.locator('#codex-gateway-status').inner_text()=='Codex网关未启用'
    assert 'inactive' in page.locator('#codex-gateway-status').get_attribute('class')
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:false,connected:false})")
    cards=page.locator('.unified-maintenance-cards > *')
    classes=[cards.nth(i).get_attribute('class') for i in range(cards.count())]
    assert classes[-3].endswith('host-control-card') and classes[-2]=='advanced-card claude-desktop-card' and classes[-1]=='advanced-card bridge-card',classes
    assert 'Claude Desktop 接入' in page.locator('.claude-desktop-card h3').inner_text()
    # The client launch controls follow active integration status.
    for card,launch in [('.claude-desktop-card','#claude-desktop-launch'),('.bridge-card','#codex-launch')]:
        assert page.locator(card+' .bridge-card-actions button').all_text_contents()==['打开客户端','设置']
        assert page.locator(card+' .client-logo').evaluate('e=>e.complete&&e.naturalWidth>0')
        assert page.locator(launch).is_disabled()
    assert page.locator('.claude-desktop-card .client-card-heading #desktop-key-settings').is_visible()
    assert '使用 API Key 或 SSH' not in page.locator('.claude-desktop-card').inner_text()
    page.evaluate("status=>window.safeCodeClaudeDesktopStatus(status)",active)
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:true,configuration:'fixture'})")
    for launch in ['#claude-desktop-launch','#codex-launch']:page.locator(launch).click()
    page.wait_for_function('window.clientOpenCalls.length===2')
    assert page.evaluate('window.clientOpenCalls')==['claudeDesktop.open','codex.open']
    assert not page.evaluate('window.claudeStartCalls.length')
    page.evaluate("window.clientOpenError='未检测到 Codex，请先安装官方客户端。'")
    page.locator('#codex-launch').click()
    page.wait_for_selector('#app-message-dialog[open]')
    assert '请先安装官方客户端' in page.locator('#app-message-dialog').inner_text()
    page.locator('#app-message-confirm').click()
    page.evaluate('window.clientOpenError=null')
    # The outline covers the viewport but does not intercept client controls.
    assert page.locator('.window').evaluate("e=>{const s=getComputedStyle(e,'::after');return s.borderTopWidth==='1px'&&s.pointerEvents==='none'&&s.position==='fixed'}")
    page.evaluate("status=>window.__claudeStatus=status",base)
    page.evaluate("window.safeCodeClaudeDesktopStatus(window.__claudeStatus)")
    assert page.locator("#claude-gateway-status").inner_text()=="Claude网关未启用"
    assert "inactive" in page.locator("#claude-gateway-status").get_attribute("class")
    page.evaluate("showScreen('login');document.querySelector('#ip').value='192.0.2.42';document.querySelector('#username').value='fixture';document.querySelector('#login-form').onsubmit({preventDefault(){}})")
    page.wait_for_function("!document.querySelector('#claude-desktop-manage').disabled")
    page.evaluate("switchMode('maintenance')")
    def open_desktop_settings():
        page.evaluate("() => { window.settingsAction=document.querySelector('#claude-desktop-manage').onclick(); }")
    for dismiss in ('#app-message-cancel','#app-message-dialog [data-dialog-dismiss]','escape'):
        open_desktop_settings()
        page.wait_for_selector('#app-message-dialog[open]')
        assert page.locator('#app-message-cancel').inner_text()=='我手滑了'
        assert page.locator('#app-message-confirm').inner_text()=='我知道我在做什么'
        assert page.locator('#app-message-body').inner_text()=='⚠️本功能未经过大范围验证，如果担心你的账户安全，请不要启用，除非您有非用不可桌面端的需求，否则请使用CloudCLI 网页界面'
        assert not page.locator('#claude-desktop-dialog').is_visible()
        if dismiss=='escape':page.keyboard.press('Escape')
        else:page.locator(dismiss).click()
        page.evaluate('window.settingsAction')
        assert page.evaluate('window.claudeWarningAccepts')==0
        assert not page.locator('#claude-desktop-dialog').is_visible()
    open_desktop_settings()
    page.wait_for_selector('#app-message-dialog[open]')
    page.locator('#app-message-confirm').click()
    page.locator('#host-dialog').wait_for()
    assert not page.locator('#host-risk').is_checked()
    assert page.locator('#host-enable').is_disabled()
    assert not page.locator('#claude-desktop-dialog').is_visible()
    assert page.evaluate('window.claudeStartCalls.length')==0
    page.locator('#host-close').click()
    page.evaluate('window.settingsAction')
    assert not page.locator('#claude-desktop-dialog').is_visible()
    open_desktop_settings()
    page.locator('#host-dialog').wait_for()
    page.evaluate("window.hostEnableError='本机登录未验证'")
    page.locator('#host-risk').check()
    page.locator('#host-enable').click()
    page.get_by_text('本机登录未验证',exact=True).wait_for()
    assert not page.locator('#host-risk').is_checked()
    assert page.evaluate('window.claudeStartCalls.length')==0
    page.evaluate("window.hostEnableError=null")
    page.locator('#host-risk').check()
    page.locator('#host-enable').click()
    page.locator('#claude-desktop-dialog').wait_for()
    page.evaluate('window.settingsAction')
    assert page.evaluate('window.claudeWarningAccepts')==1
    assert page.evaluate('window.hostEnableCalls.every(call=>call.riskAccepted===true)')
    assert not page.locator('#host-dialog').is_visible()
    assert page.evaluate('window.claudeStartCalls.length')==0
    page.wait_for_function("document.querySelector('#claude-desktop-dialog').open")
    assert page.locator('#claude-desktop-gateway').count()==0
    assert page.locator('#claude-desktop-key').count()==0
    assert page.locator('#claude-desktop-model, #claude-desktop-display').count()==0
    assert 'Claude 模型标识' not in page.locator('#claude-desktop-form').inner_text()
    assert 'VPS' in page.locator('#claude-desktop-existing').inner_text()
    assert page.locator('#claude-desktop-start').inner_text()=='部署并启用'
    page.evaluate("status=>window.__claudeActive=status",active)
    page.locator('#claude-desktop-consent').check()
    page.locator('#claude-desktop-start').click()
    page.wait_for_function('window.claudeStartCalls.length===1')
    call=page.evaluate('window.claudeStartCalls[0]')
    assert call['args']=={'accepted':True},call
    assert page.locator('#claude-gateway-status').inner_text()=='Claude网关已启用'
    assert 'inactive' not in page.locator('#claude-gateway-status').get_attribute('class')
    assert page.locator('#claude-desktop-stop').is_visible()
    assert page.locator('#claude-desktop-consent').is_disabled()
    assert page.locator('#claude-desktop-stop').inner_text()=='停止桥接'
    assert '127.0.0.1' in page.locator('#claude-desktop-existing').inner_text()
    assert 'Opus' not in page.locator('#claude-desktop-existing').inner_text()
    assert '模型权限' not in page.locator('.claude-desktop-card').inner_text()
    evidence=root/'.local/evidence/claude-desktop-scroll'
    evidence.mkdir(parents=True,exist_ok=True)
    for width,height in [(740,634),(360,600),(360,400)]:
        page.set_viewport_size({'width':width,'height':height})
        content=page.locator('.claude-desktop-content')
        footer=page.locator('#claude-desktop-form .config-footer')
        header=page.locator('#claude-desktop-dialog>.titlebar')
        if height<=400:assert content.evaluate('e=>e.scrollHeight>e.clientHeight')
        for fraction in (0,.5,1):
            content.evaluate('(e,f)=>e.scrollTop=(e.scrollHeight-e.clientHeight)*f',fraction)
            page.wait_for_timeout(50)
            rects=[locator.bounding_box() for locator in (header,content,footer)]
            top,body,bottom=rects
            assert top['y']+top['height']<=body['y']+1,rects
            assert body['y']+body['height']<=bottom['y']+1,rects
            button=page.locator('#claude-desktop-stop').bounding_box()
            assert button['y']>=bottom['y'] and button['y']+button['height']<=height,rects
            assert footer.evaluate("e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}")
        # The client entry remains fully reachable above the footer at the bottom.
        opened=page.locator('#claude-desktop-open').bounding_box()
        assert opened['y']>=body['y'] and opened['y']+opened['height']<=bottom['y'],(opened,rects)
        page.screenshot(path=str(evidence/f'active-{width}x{height}.png'))
    before=footer.bounding_box()
    content.hover();page.mouse.wheel(0,-10000)
    page.wait_for_function("document.querySelector('.claude-desktop-content').scrollTop===0")
    page.wait_for_timeout(300)
    after=footer.bounding_box()
    assert abs(before['y']-after['y'])<1,(before,after)
    page.set_viewport_size({'width':1000,'height':900})
    attention={**active,'active':False,'state':'attention','message':'SSH gateway unavailable'}
    page.evaluate("status=>window.safeCodeClaudeDesktopStatus(status)",attention)
    assert page.locator('#claude-desktop-stop').is_visible()
    assert page.locator('#claude-gateway-status').inner_text()=='Claude网关未启用'
    assert 'inactive' in page.locator('#claude-gateway-status').get_attribute('class')
    stopped={**base,'configured':False,'message':'VPS 桥接已停止，请重新启用。'}
    page.evaluate("status=>window.__claudeStopped=status",stopped)
    page.evaluate("document.querySelector('#claude-desktop-stop').onclick()")
    page.wait_for_function('window.claudeStopCalls===1')
    page.wait_for_function("!document.querySelector('#claude-desktop-consent').checked")
    assert page.locator('#claude-desktop-start').is_visible()
    page.set_viewport_size({'width':360,'height':600})
    box=page.locator('#claude-desktop-dialog').bounding_box()
    assert box['x']>=0 and box['x']+box['width']<=360
    assert page.locator('#claude-desktop-form').evaluate('e=>e.scrollWidth<=e.clientWidth')
    page.locator('#claude-desktop-close').click()
    open_desktop_settings()
    page.evaluate('window.settingsAction')
    assert page.locator('#claude-desktop-dialog').is_visible()
    assert not page.locator('#app-message-dialog').is_visible()
    assert page.evaluate('window.claudeWarningAccepts')==1
    page.locator('#claude-desktop-close').click()
    page.evaluate("showScreen('login')")
    page.locator('#advanced-connect').click()
    page.locator('#enter-preview').click()
    page.evaluate("switchMode('maintenance')")
    before=page.evaluate('window.clientOpenCalls.length')
    assert page.locator('#claude-desktop-launch').is_disabled()
    assert page.evaluate('window.clientOpenCalls.length')==before
    evidence=root/'.local/evidence/client-cards'
    evidence.mkdir(parents=True,exist_ok=True)
    for width,height in [(740,634),(360,600)]:
        page.set_viewport_size({'width':width,'height':height})
        page.locator('#codex-launch').scroll_into_view_if_needed()
        assert page.locator('.gateway-statuses').evaluate('''e=>{
          const group=e.getBoundingClientRect(), footer=e.parentElement.getBoundingClientRect();
          const children=[...e.children].map(x=>x.getBoundingClientRect());
          const delta=Math.abs(group.left+group.width/2-footer.left-footer.width/2);
          return delta<1&&children.every(x=>Math.abs(x.top-children[0].top)<1&&Math.abs(x.height-children[0].height)<1);
        }''')
        for card in ['.claude-desktop-card','.bridge-card']:
            assert page.locator(card).evaluate('e=>e.scrollWidth<=e.clientWidth')
            buttons=page.locator(card+' .bridge-card-actions button')
            left,right=buttons.nth(0).bounding_box(),buttons.nth(1).bounding_box()
            assert left['x']+left['width']<=right['x'],(left,right)
        page.screenshot(path=str(evidence/f'preview-{width}x{height}.png'))
        if width==740:
            first=page.locator('.claude-desktop-card').bounding_box()
            last=page.locator('.bridge-card').bounding_box()
            page.screenshot(path=str(evidence/'cards.png'),clip={'x':first['x'],'y':first['y'],
                'width':first['width'],'height':last['y']+last['height']-first['y']})
    assert not errors,errors
    (root/'.local/evidence').mkdir(exist_ok=True)
    page.screenshot(path=str(root/'.local/evidence/claude-desktop-vps-ui.png'))
    browser.close()
print('CLIENT_LAUNCH_GATING_ICONS_BORDER_AND_DESKTOP_DIALOG_OK')
