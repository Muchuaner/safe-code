"""macOS UI parity and disabled Windows adapter; no model or SSH requests."""
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=browser.new_page(viewport={'width':740,'height':634});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 main_fixture='''window.__safeCodeDesktop=true;window.__safeCodePlatform='macos';window.__safeCodeAcceptance=true;window.calls=[];
 window.gatewayOnline=true;window.hostEnabled=false;
 window.publicGatewayFixture=null;
 window.claudeInstalled=true;window.cloudInstalled=false;
 window.quotaFixture={state:'available',windows:{five_hour:{used:21,resets_at:1929168000},seven_day:{used:92}}};
 window.chrome={webview:{postMessage(m){
   calls.push(m);
   if(m.op==='host.enable')window.hostEnabled=m.args.riskAccepted===true;
   const needsKey=m.op==='safeCodeApi.install'&&window.cloudInstalled&&!window.publicGatewayFixture?.cloudcliConfigured&&!m.args.cloudcli;
   const generatesAccount=m.op==='safeCodeApi.install'&&!window.cloudInstalled&&window.publicGatewayFixture?.cloudcliConfigured===false;
   if(m.op==='safeCodeApi.install'&&!needsKey){
     window.cloudInstalled=true;
     window.publicGatewayFixture={enabled:window.publicGatewayFixture?.enabled||false,backend:'cloudcli',gatewayRevision:10,cloudcliConfigured:true};
   }
   if(m.op==='safeCodeApi.enablePublic')window.publicGatewayFixture={enabled:true,backend:'cloudcli',gatewayRevision:10,cloudcliConfigured:true};
   if(m.op==='cloud.install')window.cloudInstalled=true;
   const result=m.op==='services.preferences'?{enabled:false,serviceAvailable:false}:
     m.op==='desktopKey.status'?{configured:false,running:false}:
     m.op==='notice.state'?{accepted:false}:
     m.op==='login.restoreAvailable'?true:
     m.op==='login.restoreSaved'?{host:'192.0.2.42',username:'fixture',password:'fixture-only',port:22}:
     ['host.status','host.enable'].includes(m.op)?{enabled:window.hostEnabled,state:window.hostEnabled?'connected':'disabled',alias:'mac',command:'',message:'未启用'}:
     m.op==='bridge.status'?{running:false}:
     needsKey?{cloudcliKeyRequired:true}:
     generatesAccount?{...window.publicGatewayFixture,cloudcliAccount:{username:'generated-fixture',password:'generated-fixture-password'}}:
     ['safeCodeApi.publicStatus','safeCodeApi.install','safeCodeApi.enablePublic'].includes(m.op)?
       window.publicGatewayFixture||{enabled:true,backend:'cloudcli',gatewayRevision:10,active:1,concurrency:1,waiting:2,subscription:window.quotaFixture}:
     m.op==='window.gateway.open'?true:
     m.op==='connect'?{server:{statuses:{account:true,claude:window.claudeInstalled},cloudReady:window.cloudInstalled,assignedIps:[],osVersion:'24.04'}}:
     m.op==='cloud.install'?{statuses:{account:true},cloudReady:true,assignedIps:[],osVersion:'24.04'}:null;
   const error=m.op==='safeCodeApi.publicStatus'&&!window.gatewayOnline?'网络中断':undefined;
   if(m.op==='safeCodeApi.install'&&window.deferGatewayInstall){
     window.replyToGatewayInstall=()=>window.safeCodeNativeResponse?.({id:m.id,result,error});
     return;
   }
   queueMicrotask(()=>window.safeCodeNativeResponse?.({id:m.id,result,error}));
 }}};'''
 page.add_init_script(main_fixture)
 page.goto((root/'web'/'index.html').as_uri());page.wait_for_function('window.safeCodeNativeReady===true')
 page.evaluate("showScreen('workspace');switchMode('maintenance')")
 assert page.locator('.window-controls').is_hidden();assert page.locator('#codex-launch').is_disabled();assert page.locator('#claude-desktop-launch').is_disabled();assert '暂不可用' in page.locator('#bridge-state').inner_text()
 assert 'macOS' in page.locator('#usage-detail').inner_text();assert '系统设置' in page.locator('.host-risk small').inner_text()
 assert page.locator('#score-source').get_attribute('href') is None
 assert page.locator('#mount-guard-status').evaluate('el=>getComputedStyle(el).backgroundColor')=='rgb(255, 248, 220)'
 page.evaluate("safeCodeMountStatus({count:1,state:'connected',message:'Connected'})")
 page.wait_for_function("getComputedStyle(document.querySelector('#mount-guard-status')).backgroundColor==='rgb(247, 250, 246)'")
 page.locator('#bridge-manage').click()
 page.wait_for_selector('#app-message-dialog[open]')
 assert '自行承担后果' in page.locator('#app-message-body').inner_text()
 assert 'macOS 暂不可用' in page.locator('#app-message-body').inner_text()
 page.locator('#app-message-confirm').click()
 assert page.locator('#bridge-dialog').is_hidden()
 # The original password remains available after an explicit Keychain unlock.
 page.evaluate("showScreen('login')")
 restore=page.locator('#restore-saved-login')
 expect(restore).to_have_text('解锁已保存的登录信息')
 restore.click()
 expect(page.locator('#ip')).to_have_value('192.0.2.42')
 expect(page.locator('#username')).to_have_value('fixture')
 expect(page.locator('#password')).to_have_value('fixture-only')
 expect(restore).to_be_hidden()
 page.locator('#connect').click()
 page.wait_for_function("!document.querySelector('#claude-desktop-manage').disabled")
 assert page.locator('#public-gateway-runtime').is_hidden()
 assert page.locator('#footer-claude-limits').is_hidden()
 assert page.evaluate("getComputedStyle(document.querySelector('.window'),'::after').content")== 'none'
 assert not page.evaluate("calls.some(m=>m.op==='safeCodeApi.refreshSubscription')")
 page.evaluate("switchMode('maintenance')")
 # A connected work account can open the host-control settings.
 page.locator('#host-manage').click()
 page.wait_for_selector('#host-dialog[open]')
 assert page.locator('#host-alias').input_value()=='mac'
 assert not page.locator('#host-risk').is_checked()
 assert '远程登录' in page.locator('#host-progress').inner_text()
 page.locator('#host-close').click()
 page.locator('#claude-desktop-manage').click()
 expect(page.locator('#claude-desktop-title')).to_have_text('Safe Code API · 公网网关')
 expect(page.locator('#safe-code-api-enable')).to_have_text('已启用')
 page.locator('#safe-code-api-manage').click()
 page.wait_for_function("calls.some(m=>m.op==='window.gateway.open')")
 expect(page.locator('#claude-desktop-dialog')).to_be_hidden()
 assert page.locator('#claude-desktop-launch').is_disabled()
 assert not page.evaluate("calls.some(m=>m.op==='host.enable'||m.op.startsWith('claudeDesktop.'))")
 assert not errors,errors

 for gateway_state,cloud_installed,needs_key in (
     ('{installed:false,enabled:false,cloudcliConfigured:false}',False,False),
     ('{enabled:true,cloudcliConfigured:false}',True,True),
     ("{enabled:true,backend:'cloudcli',gatewayRevision:4,cloudcliConfigured:true}",True,False),
 ):
  newbie=browser.new_page(viewport={'width':740,'height':634})
  newbie.on('pageerror',lambda e:errors.append(str(e)))
  flags='window.publicGatewayFixture='+gateway_state+';window.cloudInstalled='+str(cloud_installed).lower()+';'
  newbie.add_init_script(main_fixture+';'+flags)
  newbie.goto((root/'web'/'index.html').as_uri())
  newbie.wait_for_function('window.safeCodeNativeReady===true')
  newbie.evaluate("showScreen('login')")
  newbie.locator('#restore-saved-login').click()
  newbie.locator('#connect').click()
  newbie.wait_for_function("!document.querySelector('#claude-desktop-manage').disabled")
  newbie.evaluate("switchMode('maintenance')")
  newbie.locator('#claude-desktop-manage').click()
  expect(newbie.locator('#claude-desktop-title')).to_have_text('Safe Code API · 公网网关')
  enable=newbie.locator('#safe-code-api-enable')
  expect(enable).to_have_text('启用')
  expect(newbie.locator('#safe-code-cloudcli-config')).to_be_hidden()
  if not cloud_installed:
   newbie.evaluate('window.deferGatewayInstall=true')
  enable.click()
  if needs_key:
   expect(newbie.locator('#safe-code-cloudcli-config')).to_be_visible()
   expect(newbie.locator('#safe-code-cloudcli-config input')).to_have_count(1)
   newbie.locator('#safe-code-cloudcli-api-key').fill('fixture-cloudcli-key')
   enable.click()
   newbie.wait_for_function("calls.filter(message=>message.op==='safeCodeApi.install').length===2")
   assert newbie.evaluate("calls.filter(message=>message.op==='safeCodeApi.install').at(-1).args.cloudcli")=={'apiKey':'fixture-cloudcli-key'}
  elif not cloud_installed:
   newbie.wait_for_function('!!window.replyToGatewayInstall')
   expect(newbie.locator('#safe-code-api-progress')).to_contain_text('可以关闭此窗口')
   newbie.locator('#claude-desktop-close').click()
   expect(newbie.locator('#claude-desktop-dialog')).to_be_hidden()
   newbie.evaluate('window.replyToGatewayInstall()')
   expect(enable).to_have_text('已启用')
   expect(newbie.locator('#app-message-dialog')).to_be_hidden()
   newbie.locator('#claude-desktop-manage').click()
   expect(newbie.locator('#app-message-title')).to_have_text('CloudCLI 账户已创建')
   expect(newbie.locator('#app-message-body')).to_contain_text('generated-fixture-password')
   newbie.locator('#app-message-confirm').click()
   expect(newbie.locator('#app-message-body')).to_be_empty()
   assert newbie.evaluate("calls.filter(message=>message.op==='safeCodeApi.enablePublic').map(message=>message.args)")==[{'accepted':True}]
  expect(enable).to_have_text('已启用')
  if not needs_key:
   assert newbie.evaluate("calls.filter(message=>message.op==='safeCodeApi.install').map(message=>message.args)")==[{'accepted':True}]
  expect(newbie.locator('#safe-code-cloudcli-api-key')).to_have_value('')
  newbie.locator('#safe-code-api-manage').click()
  newbie.wait_for_function("calls.some(message=>message.op==='window.gateway.open')")
  expect(newbie.locator('#claude-desktop-dialog')).to_be_hidden()
  assert not newbie.evaluate("calls.some(message=>message.op==='host.enable'||message.op.startsWith('claudeDesktop.'))")
  newbie.close()
 assert not errors,errors

 resumed=browser.new_page(viewport={'width':740,'height':634})
 resumed.on('pageerror',lambda e:errors.append(str(e)))
 resumed.clock.install()
 resumed.add_init_script(main_fixture.replace("window.__safeCodeAcceptance=true;", "window.resumeAttempts=0;")
   .replace("m.op==='notice.state'?{accepted:false}:", """m.op==='notice.state'?{accepted:true}:
     m.op==='gateways.resume-settings'?{host:'192.0.2.42',username:'fixture',password:'fixture-only',port:22}:""")
   .replace("m.op==='connect'?{server:", """m.op==='connect'&&++window.resumeAttempts===1?{retryable:true}:
     m.op==='connect'?{server:"""))
 resumed.goto((root/'web'/'index.html').as_uri())
 expect(resumed.locator('#connection-progress-text')).to_contain_text('5 秒后自动重连')
 resumed.clock.run_for(5100)
 expect(resumed.locator('#public-gateway-runtime')).to_be_hidden()
 resumed.wait_for_function("calls.some(message=>message.op==='safeCodeApi.publicStatus')")
 assert resumed.evaluate("calls.filter(message=>message.op==='connect').every(message=>message.args.automatic===true)")
 assert not resumed.evaluate("calls.some(message=>message.op.startsWith('notice.claudeDesktop.'))")
 assert resumed.locator('#app-message-dialog').is_hidden()
 resumed.close()

 gateway=browser.new_page(viewport={'width':1100,'height':670})
 gateway.on('pageerror',lambda e:errors.append(str(e)))
 gateway.add_init_script("""window.calls=[];
 const id='a'.repeat(64);
 const key={id,note:'旧设备',rpm:0,active:true,enabled:true,waiting:1,requests:2,tokens:18};
 const records=[{key_id:id,model:'claude-test',at:Math.floor(Date.now()/1000),outcome:'success',status:200,wait_ms:82,duration_ms:1600,tokens:18}];
 const models=[{model:'claude-test',requests:2,failures:0,input:10,output:3,cache_read:5,cache_write:0}];
 const keys=[key];
 function analytics(period){
   if(!period)return undefined;
   const [step,count]={ '1h':[300,12], '24h':[3600,24], '7d':[86400,7] }[period];
   return {period,start:Math.floor(Date.now()/1000)-step*count,step,count,
     timeline:[{bucket:2,requests:2,tokens:24,failed:0,limited:1}],
     outcomes:[{outcome:'success',status:200,requests:1},{outcome:'limited',status:429,requests:1}],
     latency_limits:[250,500,1000,2500,5000,10000],
     latency:{duration_ms:{samples:1,average_ms:1600,p95_ms:1600,histogram:[0,0,0,1,0,0,0]},
              wait_ms:{samples:2,average_ms:82,p95_ms:82,histogram:[2,0,0,0,0,0,0]}}};
 }
 function rpc(op,args){
   if(op==='copy')return args.text==='sc1.disposable-fixture-only';
   if(args.action==='list')return {keys,active:true,waiting:1,origin:'https://api.fixture.invalid',
     subscription:{state:'available',windows:{five_hour:{used:27.5},seven_day:{used:92}}},
     stats:{requests:1,failures:0,limited:0,tokens:18,rpm:1,tpm:18,average_ms:1600},
     models:[{...models[0],tokens:18}],analytics:analytics(args.analytics),recent:records};
   if(args.action==='usage')return {models,recent:records};
   if(args.action==='issue'){
     keys.push({id:'b'.repeat(64),note:args.note,rpm:args.rpm,enabled:true,requests:0,waiting:0,active:false,tokens:0});
     return {id:'b'.repeat(64),ticket:'sc1.disposable-fixture-only'};
   }
   const target=keys.find(key=>key.id===args.id);
   if(args.action==='update')Object.assign(target,{note:args.note,rpm:args.rpm});
   if(args.action==='revoke')target.enabled=false;
   return {id:args.id};
 }
 window.chrome={webview:{postMessage({id,op,args}){
   calls.push({op,args});
   queueMicrotask(()=>window.safeCodeNativeResponse({id,result:rpc(op,args)}));
 }}};""")
 gateway.goto((root/'web'/'gateway.html').as_uri())
 expect(gateway.locator('#metric-queue')).to_have_text('1 / 1')
 expect(gateway.locator('#gateway-claude-limits [data-limit=five_hour]')).to_have_text('72.5%')
 assert gateway.locator('dialog[open]').count()==0
 assert gateway.locator('#queue-current').inner_text()=='正在处理 · 旧设备'
 assert '排队 1' in gateway.locator('.key-activity').inner_text()
 assert gateway.locator('#key-rows tr').count()==1
 assert gateway.evaluate('document.documentElement.scrollWidth===innerWidth')
 gateway.locator('#key-search').fill('不存在')
 assert gateway.locator('#key-empty').is_visible()
 gateway.locator('#key-search').fill('')
 gateway.get_by_role('button',name='用量详情').click()
 gateway.wait_for_selector('#usage-rows tr')
 assert gateway.locator('#usage-rows td').nth(7).inner_text()=='18'
 gateway.get_by_role('button',name='查看此密钥的请求').click()
 assert '82 毫秒' in gateway.locator('#request-rows').inner_text()
 assert '1.60 秒' in gateway.locator('#request-rows').inner_text()
 gateway.get_by_role('button',name='用量分析').click()
 expect(gateway.locator('#traffic-chart')).to_be_visible()
 expect(gateway.locator('#outcome-chart .donut strong')).to_have_text('50%')
 expect(gateway.locator('#latency-summary')).to_contain_text('P95 1.60 秒')
 gateway.locator('#latency-metric').select_option('wait_ms')
 expect(gateway.locator('#latency-summary')).to_contain_text('P95 82 毫秒')
 gateway.get_by_role('button',name='近 7 天').click()
 expect(gateway.locator('#traffic-summary')).to_have_text('近 7 天 · 每段 1 天')
 gateway.locator('#traffic-chart canvas').focus()
 gateway.locator('#traffic-chart canvas').press('Home')
 expect(gateway.locator('#traffic-chart .chart-readout')).to_contain_text('Token / 分钟')
 expect(gateway.locator('#analysis-queue')).to_contain_text('排队 1')
 assert 'claude-test' in gateway.locator('#model-rows').inner_text()
 gateway.get_by_role('button',name='密钥管理').click()
 gateway.locator('#create-key').click()
 gateway.locator('#note').fill('测试笔记本')
 gateway.locator('#rpm').fill('8')
 gateway.locator('#save-key').click()
 gateway.wait_for_selector('#issued-dialog[open]')
 assert gateway.locator('#ticket').input_value()=='sc1.disposable-fixture-only'
 gateway.locator('#copy-key').click()
 expect(gateway.locator('#copy-status')).to_have_text('已复制密钥。')
 gateway.get_by_role('button',name='完成').click()
 expect(gateway.locator('#ticket')).to_have_value('')
 first=gateway.locator('#key-rows tr').filter(has_text='旧设备')
 first.get_by_role('button',name='编辑').click()
 expect(gateway.locator('#editor-title')).to_have_text('编辑密钥')
 gateway.locator('#rpm').fill('3')
 gateway.locator('#save-key').click()
 first.get_by_text('3 次/分').wait_for()
 first.get_by_role('button',name='撤销').click()
 gateway.locator('#revoke-key').click()
 first.get_by_text('已撤销').wait_for()
 gateway.locator('#key-filter').select_option('enabled')
 assert gateway.locator('#key-rows tr').count()==1
 assert not errors,errors;browser.close()
print('MAC_NATIVE_UI_REMOTE_LOGIN_AND_PUBLIC_GATEWAY_SETTINGS_OK')
