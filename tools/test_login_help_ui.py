"""Login help, account confirmation and in-place credential UI, using isolated browser RPC fixtures."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
evidence=root/'.local/evidence/login-recovery';evidence.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page(viewport={'width':360,'height':536});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if not r.request.url.startswith('https://safe-code.local/'):r.abort();return
  path=root/r.request.url.split('safe-code.local/',1)[1].split('?',1)[0]
  r.fulfill(path=str(path)) if path.is_file() else r.fulfill(status=404,body='')
 page.route('**/*',route)
 page.add_init_script('''
 window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.calls=[];window.savedDraft=null;window.accountUsed=false;
 window.server={osId:'ubuntu',osVersion:'24.04',hostname:'fixture-host',username:'workspace',homeDirectory:'/home/workspace',statuses:{},assignedIps:[],mountReady:true};
 window.chrome={webview:{postMessage(message){
   window.calls.push(message);let result=null,error=null;
   switch(message.op){
    case 'services.preferences':result={enabled:false,serviceAvailable:false};break;
    case 'notice.state':result={accepted:true};break;
    case 'connect':result={ready:true,server:window.server};break;
    case 'account.draft.get':result=window.savedDraft;break;
    case 'deployment.settings.get':result={accountSaved:window.accountUsed,localeRegion:window.localeRegion||null};break;
    case 'deployment.locale.save':window.localeRegion=message.args.region;result={accountSaved:window.accountUsed,localeRegion:window.localeRegion};break;
    case 'account.change.status':result={requiresConfirmation:window.accountUsed};break;
    case 'account.draft.save':window.savedDraft=message.args;window.accountUsed=true;result=true;break;
    case 'deploy':window.server.statuses.account=true;window.savedDraft=null;result=window.server;break;
    case 'metrics':result={latencyMs:40,lossPercent:0};break;
    case 'connection.info':result={host:'203.0.113.86',port:22,username:'root'};break;
    case 'connection.reauthenticate':if(message.args.password==='wrong'){error='SSH 登录认证失败';}else result={ready:true,mount:{count:1,state:'connected',message:'挂载守护中'}};break;
   }
   queueMicrotask(()=>window.safeCodeNativeResponse({id:message.id,result,error}));
 }}};
 ''')
 page.goto('https://safe-code.local/web/index.html');page.wait_for_function('window.safeCodeNativeReady');page.evaluate("showScreen('login')")
 a=page.locator('#advanced-connect').bounding_box();b=page.locator('#login-help').bounding_box();main=page.locator('#connect').bounding_box()
 assert abs(a['width']-main['width']/2)<10 and b['x']>a['x']+a['width']
 assert b['y']+b['height']<536 and page.locator('#back').bounding_box()['y']<536
 assert abs(a['width']-b['width'])<1 and abs(a['height']-b['height'])<1
 assert page.locator('#back').bounding_box()['y']+page.locator('#back').bounding_box()['height']<=520
 page.screenshot(path=str(evidence/'login.png'))
 page.set_viewport_size({'width':360,'height':500})
 page.screenshot(path=str(evidence/'login-small.png'))
 assert page.locator('#back').bounding_box()['y']+page.locator('#back').bounding_box()['height']<=490, page.locator('#back').bounding_box()
 page.set_viewport_size({'width':360,'height':536})
 page.locator('#login-help').click();page.set_viewport_size({'width':740,'height':634});page.wait_for_function("document.querySelector('#login-help-dialog img').naturalWidth>0")
 page.screenshot(path=str(evidence/'tutorial.png'));page.get_by_role('button',name='关闭教程').click()
 page.locator('#advanced-connect').click();assert page.locator('#advanced-dialog').is_visible();page.locator('#advanced-dialog .close').click()
 page.locator('#ip').fill('203.0.113.86');page.locator('#username').fill('root');page.locator('#password').fill('fixture-old');page.locator('#connect').click();page.wait_for_function("!document.querySelector('#update-login-password').hidden")
 page.get_by_role('button',name='自定义工作用户和机器名称').click();page.locator('#account-settings-form [type=submit]').click()
 page.wait_for_function("window.calls.filter(x=>x.op==='account.draft.save').length===1");assert not page.locator('#app-message-dialog').is_visible()
 assert page.evaluate("window.calls.filter(x=>x.op==='deploy').length")==0
 assert page.locator('#deploy-all').is_disabled()
 page.get_by_role('button',name='自定义本土化').click();page.locator('#locale-region').select_option('jp')
 page.locator('#localization-form [type=submit]').click();page.wait_for_function("!document.querySelector('#deploy-all').disabled")
 page.get_by_role('button',name='自定义工作用户和机器名称').click();page.locator('#custom-password').fill('fixture-new-password');page.locator('#account-settings-form [type=submit]').click()
 page.get_by_role('button',name='我不改了',exact=True).wait_for();page.screenshot(path=str(evidence/'account-warning.png'));page.get_by_role('button',name='我不改了',exact=True).click()
 page.wait_for_function("!document.querySelector('#account-settings-form [type=submit]').disabled")
 assert page.evaluate("window.calls.filter(x=>x.op==='account.draft.save').length")==1
 page.locator('#account-settings-form [type=submit]').click();page.get_by_role('button',name='我知道自己在做什么，更改并应用',exact=True).click()
 page.wait_for_function("window.calls.filter(x=>x.op==='deploy').length===1")
 assert page.evaluate("window.calls.find(x=>x.op==='deploy').args.password")=='fixture-new-password'
 page.wait_for_function("!document.querySelector('#update-login-password').hidden && !document.querySelector('#steps button').disabled")
 assert page.locator('#steps .locale-settings-button').evaluate_all("els=>els.every(el=>getComputedStyle(el).borderTopColor!=='rgba(0, 0, 0, 0)' && getComputedStyle(el).borderTopWidth==='1px')")
 page.screenshot(path=str(evidence/'deployment.png'))
 page.locator('#update-login-password').click();page.locator('#credential-password').fill('wrong');page.locator('#credential-form [type=submit]').click();page.wait_for_function("document.querySelector('#credential-error').textContent.includes('认证失败')")
 assert page.locator('#credential-password').input_value()=='' and page.locator('#credential-dialog').is_visible()
 page.locator('#credential-password').fill('fixture-root-new');page.screenshot(path=str(evidence/'credential.png'));page.locator('#credential-form [type=submit]').click()
 page.wait_for_function("!document.querySelector('#credential-dialog').open");page.locator('#app-message-confirm').click()
 page.evaluate("window.safeCodeMountStatus({count:1,state:'attention',message:'SSH 登录认证失败',authenticationRequired:true})")
 assert 'needs-credentials' in page.locator('#update-login-password').get_attribute('class')
 assert not errors,errors
 browser.close()
print('LOGIN_HELP_ACCOUNT_FIRST_SAVE_CANCEL_APPLY_AND_CREDENTIAL_RECOVERY_UI_PASSED')
