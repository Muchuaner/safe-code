"""Claude Desktop 接入卡片/彈出視窗夾具；不讀寫真實註冊表，不發模型請求。"""
from pathlib import Path
import os
from playwright.sync_api import expect, sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('SAFECODE_TEST_BROWSER','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),headless=False,args=['--headless=new'])
    page=browser.new_page(viewport={'width':1000,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.calls=[];window.clientOpenCalls=[];window.clientOpenError=null;
      window.keyEnableCalls=[];window.keyDisableCalls=0;
      window.keyAuthorizations=[];window.keyCanceled=0;
      const hostDisabled={enabled:false,state:'disabled',alias:'mac',command:'',message:'未啟用'};
      window.hostState=hostDisabled;window.hostEnableCalls=[];window.hostEnableError=null;
      window.chrome={webview:{postMessage(m){
        calls.push(m);
        if(m.op==='connect'){
          const server={statuses:{account:true},osVersion:'24.04',assignedIps:[],observedEgressIp:''};
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:{server}}));return;
        }
        if(m.op==='host.enable'){
          window.hostEnableCalls.push(m.args);
          if(window.hostEnableError){
            queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,error:window.hostEnableError}));return;
          }
          window.hostState={enabled:true,state:'connected',alias:m.args.alias,command:'ssh fixture',message:'已通過登入驗證'};
        }
        if(m.op==='host.status'||m.op==='host.enable'){
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.hostState}));return;
        }
        if(m.op==='safeCodeApi.publicStatus'&&window.holdGatewayStatus){window.heldGatewayStatus=m.id;return;}
        if(m.op==='safeCodeApi.install'&&window.holdGatewayInstall){window.heldGatewayInstall=m.id;return;}
        if(m.op==='window.gateway.open'&&window.holdGatewayManagement)return;
        if(m.op==='safeCodeApi.publicStatus'||m.op==='safeCodeApi.enablePublic'){
          if(m.op==='safeCodeApi.enablePublic')window.gatewayEnabled=true;
          const result=window.gatewayMissing?{installed:false,enabled:false}:
            {enabled:!!window.gatewayEnabled,backend:'cloudcli',gatewayRevision:12};
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result}));return;
        }
        if(m.op.startsWith('desktopKey.')){
          if(m.op==='desktopKey.status'&&window.keyStatusFailure){
            queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,...window.keyStatusFailure}));return;
          }
          if(m.op==='desktopKey.enable'){
            window.keyEnableCalls.push(m.args);
            if(m.args.key==='sc1.own-vps'){
              const result={requiresDevice:true,reuseControl:true,existingControl:true,alias:'win-fixture'};
              queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result}));return;
            }
            if(m.args.useSaved? !window.keyConfigured : !['sc1.fixture','sc1.replacement'].includes(m.args.key)){
              queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,error:'Key 無效或已撤銷。'}));return;
            }
            window.keyConfigured=true;window.keyRunning=true;window.keyStatusFailure=null;
          }
          if(m.op==='desktopKey.authorizeDevice'){
            window.keyAuthorizations.push(m.args);
            window.keyConfigured=true;window.keyRunning=true;window.keyStatusFailure=null;
          }
          if(m.op==='desktopKey.cancelDevice')window.keyCanceled++;
          if(m.op==='desktopKey.disable'){window.keyDisableCalls++;window.keyConfigured=false;window.keyRunning=false;}
          queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:{configured:!!window.keyConfigured,running:!!window.keyRunning,deviceAlias:window.keyRunning?'mac-fixture':undefined}}));return;
        }
        if(m.op==='claudeDesktop.open'||m.op==='codex.open'){window.clientOpenCalls.push(m.op);queueMicrotask(()=>window.safeCodeNativeResponse({id:m.id,result:window.clientOpenResult||'已請求開啟客戶端。',error:window.clientOpenError}));return;}
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
    page.evaluate("window.keyStatusFailure={error:'暫時無法讀取接入狀態'}")
    page.locator('#desktop-key-login').click()
    page.get_by_text('暫時無法讀取接入狀態').wait_for()
    assert page.locator('#desktop-key-input').is_visible()
    page.locator('#desktop-key-close').click()
    page.evaluate("window.keyStatusFailure=null")
    page.locator('#desktop-key-login').click()
    page.locator('#desktop-key-input').fill('sc1.fixture')
    page.locator('#desktop-key-enable').click()
    page.locator('#api-manage-key').click()
    expect(page.locator('#desktop-key-open')).to_be_visible()
    assert page.locator('#desktop-key-input').input_value()==''
    assert page.locator('#desktop-key-form .config-description br').count()==1
    assert page.locator('#desktop-key-progress').inner_text().startswith('網關已就緒。\n工具目標：已授權的本機裝置 mac-fixture\n首次接入')
    assert page.locator('#desktop-key-open').inner_text()=='開啟\nClaude Desktop'
    page.locator('#desktop-key-refresh').click()
    expect(page.locator('#desktop-key-progress')).to_contain_text('模型列表已更新')
    assert page.evaluate("calls.filter(m=>m.op==='desktopKey.refreshModels').map(m=>m.args)")==[{}]
    assert page.evaluate("window.keyEnableCalls.length===1 && window.keyDisableCalls===0")
    page.locator('#desktop-key-close').click()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
    page.evaluate("window.keyRunning=false;window.keyStatusFailure={result:{configured:true,running:false,error:'本機策略無法訪問'}}")
    workspace_key=page.locator('#desktop-key-workspace')
    for width in (1000,360):
        page.set_viewport_size({'width':width,'height':900})
        button=workspace_key.bounding_box()
        maintenance=page.locator('#maintenance-mode').bounding_box()
        assert button['y']>=maintenance['y']+maintenance['height']
    page.set_viewport_size({'width':1000,'height':900})
    workspace_key.click()
    page.locator('#api-manage-key').click()
    page.get_by_text('已儲存 Key，暫未恢復接入。',exact=False).wait_for()
    assert not page.locator('#desktop-key-input').is_visible()
    assert page.locator('#desktop-key-disable').is_enabled()
    assert page.locator('#desktop-key-replace').is_enabled() and page.locator('#desktop-key-resume').is_enabled()
    assert not page.locator('#desktop-key-open').is_visible()
    page.locator('#desktop-key-resume').click()
    page.locator('#api-manage-key').click()
    page.locator('#desktop-key-open').wait_for()
    assert page.evaluate('window.keyEnableCalls.at(-1)')=={'useSaved':True}
    page.locator('#desktop-key-close').click()
    page.evaluate("window.keyStatusFailure=null")
    page.locator('#api-manage-key').click()
    page.locator('#desktop-key-replace').click()
    assert page.locator('#desktop-key-input').evaluate('e=>document.activeElement===e')
    assert page.locator('#desktop-key-enable').inner_text()=='驗證並更換'
    page.locator('#desktop-key-input').fill('sc1.invalid')
    page.locator('#desktop-key-enable').click()
    page.get_by_text('Key 無效或已撤銷。',exact=True).wait_for()
    assert page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-input').input_value()==''
    assert page.evaluate('window.keyRunning && window.keyDisableCalls===0')
    page.locator('#desktop-key-replace').click()
    assert not page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-open').is_visible()
    page.locator('#desktop-key-replace').click()
    page.locator('#desktop-key-input').fill('sc1.unsent')
    page.locator('#desktop-key-close').click()
    page.locator('#api-manage-key').click()
    assert not page.locator('#desktop-key-input').is_visible() and page.locator('#desktop-key-input').input_value()==''
    page.locator('#desktop-key-replace').click()
    page.locator('#desktop-key-input').fill('sc1.replacement')
    page.locator('#desktop-key-enable').click()
    page.locator('#api-manage-key').click()
    page.locator('#desktop-key-open').wait_for()
    assert page.evaluate('window.keyEnableCalls.at(-1)')=={'key':'sc1.replacement','replace':True}
    assert page.evaluate('window.keyDisableCalls===0')
    page.locator('#desktop-key-disable').click()
    expect(page.locator('#desktop-key-dialog')).to_be_hidden()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
    page.evaluate("window.hostState={enabled:true,state:'connected',alias:'win-fixture',command:'ssh fixture'}")
    workspace_key.click()
    page.locator('#desktop-key-input').fill('sc1.own-vps')
    page.locator('#desktop-key-enable').click()
    page.locator('#host-progress').get_by_text('這把 Key 由當前 VPS 簽發。授權後將使用已驗證的本機遠端控制通道。').wait_for()
    assert page.locator('#host-alias').input_value()=='win-fixture'
    assert page.locator('#host-enable').is_disabled()
    page.locator('#host-close').click()
    page.wait_for_function('window.keyCanceled===1')
    assert not page.locator('#desktop-key-dialog').is_visible()
    assert page.evaluate('window.keyAuthorizations.length===0')
    workspace_key.click()
    page.locator('#desktop-key-input').fill('sc1.own-vps')
    page.locator('#desktop-key-enable').click()
    page.locator('#host-risk').check()
    assert page.locator('#host-enable').inner_text()=='同意並使用'
    page.locator('#host-enable').click()
    page.locator('#api-manage-key').click()
    page.locator('#desktop-key-open').wait_for()
    assert page.evaluate('window.keyAuthorizations')==[{'riskAccepted':True}]
    assert page.evaluate('window.hostEnableCalls.length===0')
    page.locator('#desktop-key-disable').click()
    expect(page.locator('#desktop-key-dialog')).to_be_hidden()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
    page.evaluate('window.hostState={enabled:false,state:"disabled",alias:"mac",command:"",message:"未啟用"}')
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:true,configuration:'fixture'})")
    assert page.locator('#codex-gateway-status').inner_text()=='Codex網關已啟用'
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:false})")
    assert page.locator('#codex-gateway-status').inner_text()=='Codex網關未啟用'
    assert 'inactive' in page.locator('#codex-gateway-status').get_attribute('class')
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:false,connected:false})")
    cards=page.locator('.unified-maintenance-cards > *')
    classes=[cards.nth(i).get_attribute('class') for i in range(cards.count())]
    assert classes[-3].endswith('host-control-card') and classes[-2]=='advanced-card claude-desktop-card' and classes[-1]=='advanced-card bridge-card',classes
    assert 'Claude Desktop 接入' in page.locator('.claude-desktop-card h3').inner_text()
    # The client launch controls follow active integration status.
    for card,launch in [('.claude-desktop-card','#claude-desktop-launch'),('.bridge-card','#codex-launch')]:
        settings='網關設定' if card=='.claude-desktop-card' else '設定'
        assert page.locator(card+' .bridge-card-actions button').all_text_contents()==['開啟客戶端',settings]
        assert page.locator(card+' .client-logo').evaluate('e=>e.complete&&e.naturalWidth>0')
        assert page.locator(launch).is_disabled()
    assert page.locator('.claude-desktop-card .client-card-heading #desktop-key-settings').is_visible()
    assert '使用 API Key 或 SSH' not in page.locator('.claude-desktop-card').inner_text()
    workspace_key.click()
    page.locator('#desktop-key-input').fill('sc1.fixture')
    page.locator('#desktop-key-enable').click()
    expect(page.locator('#api-open-claude')).to_be_visible()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
    expect(page.locator('#claude-gateway-status')).to_have_text('網關已就緒')
    page.evaluate("window.safeCodeClaudeBridgeStatus({running:true,connected:true,configuration:'fixture'})")
    for launch in ['#claude-desktop-launch','#codex-launch']:page.locator(launch).click()
    page.wait_for_function('window.clientOpenCalls.length===2')
    assert page.evaluate('window.clientOpenCalls')==['claudeDesktop.open','codex.open']
    page.evaluate("window.clientOpenResult={installationRequired:true,message:'未找到 Claude Desktop，已開啟官方下載頁。'}")
    page.locator('#claude-desktop-launch').click()
    expect(page.locator('#app-message-dialog')).to_contain_text('已開啟官方下載頁')
    page.locator('#app-message-confirm').click()
    page.evaluate('window.clientOpenResult=null')
    page.evaluate("window.clientOpenError='未檢測到 Codex，請先安裝官方客戶端。'")
    page.locator('#codex-launch').click()
    page.wait_for_selector('#app-message-dialog[open]')
    assert '請先安裝官方客戶端' in page.locator('#app-message-dialog').inner_text()
    page.locator('#app-message-confirm').click()
    page.evaluate('window.clientOpenError=null')
    # The outline covers the viewport but does not intercept client controls.
    assert page.locator('.window').evaluate("e=>{const s=getComputedStyle(e,'::after');return s.borderTopWidth==='1px'&&s.pointerEvents==='none'&&s.position==='fixed'}")
    page.clock.install()
    page.evaluate("window.holdGatewayStatus=true")
    page.evaluate("showScreen('login');document.querySelector('#ip').value='192.0.2.42';document.querySelector('#username').value='fixture';document.querySelector('#login-form').onsubmit({preventDefault(){}})")
    page.wait_for_function("!document.querySelector('#claude-desktop-manage').disabled")
    page.evaluate("switchMode('maintenance')")
    page.locator('#claude-desktop-manage').click()
    settings=page.locator('#claude-desktop-dialog')
    footer=settings.locator('.config-footer')
    expect(footer.locator('button')).to_have_text(['故障修復','啟用','管理網關'])
    progress=page.locator('#safe-code-api-progress')
    close_settings=page.locator('#claude-desktop-close')
    enable_gateway=page.locator('#safe-code-api-enable')
    manage_gateway=page.locator('#safe-code-api-manage')
    expect(progress).to_contain_text('正在讀取公網網關狀態')
    expect(manage_gateway).to_be_disabled()
    expect(close_settings).to_be_enabled()
    close_settings.click()
    expect(settings).to_be_hidden()
    page.locator('#claude-desktop-manage').click()
    page.wait_for_function('!!window.heldGatewayStatus')
    page.clock.fast_forward(13000)
    expect(progress).to_contain_text('讀取公網網關狀態超時')
    expect(progress).to_contain_text('點擊「啟用」重試')
    expect(enable_gateway).to_be_enabled()
    page.evaluate("""safeCodeNativeResponse({
        id:heldGatewayStatus,result:{enabled:true,backend:'cloudcli',gatewayRevision:12}
    });window.holdGatewayStatus=false;window.gatewayMissing=true;window.holdGatewayInstall=true""")
    expect(enable_gateway).to_be_enabled()
    expect(manage_gateway).to_be_disabled()
    enable_gateway.click()
    expect(progress).to_contain_text('正在準備 CloudCLI 並部署公網網關')
    expect(progress).to_contain_text('可以關閉此視窗')
    close_settings.click()
    expect(settings).to_be_hidden()
    expect(workspace_key).to_be_enabled()
    page.locator('#claude-desktop-manage').click()
    expect(progress).to_contain_text('正在準備 CloudCLI 並部署公網網關')
    expect(enable_gateway).to_be_disabled()
    assert page.evaluate("calls.filter(m=>m.op==='safeCodeApi.install').length===1")
    page.evaluate("""window.gatewayMissing=false;window.holdGatewayInstall=false;
        safeCodeNativeResponse({id:heldGatewayInstall,error:'測試部署失敗。'})""")
    expect(progress).to_contain_text('測試部署失敗。')
    expect(enable_gateway).to_be_enabled()
    enable_gateway.click()
    expect(page.locator('#safe-code-api-enable')).to_have_text('已啟用')
    expect(page.locator('#safe-code-api-manage')).to_be_enabled()
    assert page.evaluate("calls.filter(m=>m.op==='safeCodeApi.enablePublic').map(m=>m.args)")==[{'accepted':True}]
    assert not page.evaluate("calls.some(m=>m.op==='host.enable'||m.op.startsWith('notice.claudeDesktop')||['claudeDesktop.start','claudeDesktop.stop','claudeDesktop.status'].includes(m.op))")
    assert not page.locator('#app-message-dialog').is_visible()
    for width,height in [(740,634),(360,600),(360,400)]:
        page.set_viewport_size({'width':width,'height':height})
        box=settings.bounding_box()
        assert box['x']>=0 and box['x']+box['width']<=width
        assert footer.evaluate('e=>e.scrollWidth<=e.clientWidth')
        manage=page.locator('#safe-code-api-manage').bounding_box()
        assert manage['y']+manage['height']<=height
    page.set_viewport_size({'width':1000,'height':900})
    page.evaluate("window.holdGatewayManagement=true")
    manage_gateway.click()
    expect(progress).to_contain_text('正在開啟網關管理')
    page.keyboard.press('Escape')
    expect(settings).to_be_hidden()
    page.locator('#claude-desktop-manage').click()
    expect(progress).to_contain_text('正在開啟網關管理')
    page.clock.fast_forward(36000)
    expect(progress).to_contain_text('開啟網關管理超時')
    expect(manage_gateway).to_be_enabled()
    page.evaluate("window.holdGatewayManagement=false")
    page.locator('#safe-code-api-manage').click()
    expect(settings).to_be_hidden()
    expect(workspace_key).to_be_visible()
    workspace_key.click()
    page.locator('#api-manage-key').click()
    page.locator('#desktop-key-disable').click()
    expect(page.locator('#desktop-key-dialog')).to_be_hidden()
    page.evaluate("showScreen('workspace');switchMode('maintenance')")
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
        assert page.locator('.gateway-statuses').evaluate('''(e,width)=>{
          const group=e.getBoundingClientRect();
          const children=[...e.children].map(x=>x.getBoundingClientRect());
          const fits=children.every(x=>x.left>=group.left&&x.right<=group.right&&x.bottom<=group.bottom);
          return fits&&(width===360||children.every(x=>Math.abs(x.top-children[0].top)<1));
        }''',width)
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
