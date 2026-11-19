"""Advanced entry, live readings, failure display and late-response isolation."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
evidence = root / '.local/evidence/connection-resources'
evidence.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
    page = browser.new_page(viewport={'width': 360, 'height': 536})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def route(request):
        url = request.request.url
        if not url.startswith('https://safe-code.local/'):
            request.abort()
            return
        file = root / url.split('safe-code.local/', 1)[1].split('?', 1)[0]
        request.fulfill(path=str(file)) if file.is_file() else request.fulfill(status=404, body='')
    page.route('**/*', route)
    page.add_init_script('''
      window.__safeCodeDesktop=true;window.__safeCodeAcceptance=true;window.__safeCodeConnectionAcceptance=true;window.calls=[];window.resourceMode='ok';window.environmentMode='ok';
      window.chrome={webview:{postMessage(message){
        calls.push(message);let result=null,error=null;
        if(message.op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
        if(message.op==='notice.state')result={accepted:true};
        if(message.op==='connect')result={server:{osVersion:'24.04',statuses:{},assignedIps:[],observedEgressIp:''}};
        if(message.op==='metrics')result={latencyMs:32,lossPercent:0};
        if(message.op==='environment.assess'){
          if(environmentMode==='hold'){window.heldEnvironment=message;return;}
          if(environmentMode==='fail')error='fixture environment unavailable';
          else result={riskScore:22,level:'low',message:'低风险',username:'workspace',signals:[{label:'系统时区',value:'America/Los_Angeles',contribution:0}]};
        }
        if(message.op==='resources'){
          if(resourceMode==='hold'){window.heldResource=message;return;}
          if(resourceMode==='fail')error='fixture unavailable';
          else result={cpuPercent:7.5,memoryUsedBytes:2*1024**3,memoryTotalBytes:8*1024**3,diskUsedBytes:12*1024**3,diskTotalBytes:60*1024**3};
        }
        queueMicrotask(()=>safeCodeNativeResponse({id:message.id,result,error}));
      }}};
    ''')
    page.clock.install()
    page.goto('https://safe-code.local/index.html')
    page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#advanced-connect').click()
    actions = page.locator('#advanced-dialog .dialog-buttons button')
    assert actions.all_text_contents() == ['保存', '直接进入', '取消']
    boxes = [actions.nth(i).bounding_box() for i in range(3)]
    assert boxes[0]['x'] < boxes[1]['x'] < boxes[2]['x']
    assert boxes[2]['x'] + boxes[2]['width'] <= 360
    assert page.locator('#ssh-port').get_attribute('type') == 'text'
    page.locator('#ssh-port').fill('65536')
    actions.nth(0).click()
    assert page.locator('#advanced-dialog').is_visible()
    page.locator('#ssh-port').fill('2222')
    actions.nth(0).click()
    assert not page.locator('#advanced-dialog').is_visible()
    page.locator('#advanced-connect').click()
    assert page.locator('#ssh-port').input_value() == '2222'
    page.screenshot(path=str(evidence / 'advanced.png'))
    assert page.locator('#ip').input_value() == ''
    page.locator('#ssh-port').fill('0')
    page.locator('#enter-preview').click()
    page.wait_for_selector('#developer-preview')
    assert page.locator('#workspace-screen').is_visible()
    assert not page.locator('#advanced-dialog').is_visible()
    assert not set(page.evaluate('calls.map(m=>m.op)')) & {'connect', 'resources', 'metrics', 'deploy', 'ip.info', 'environment.assess'}
    assert page.locator('#resource-cpu').inner_text() == '—'
    page.reload()
    page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#ip').fill('192.168.50.66')
    page.locator('#username').fill('root')
    page.locator('#password').fill('fixture')
    page.locator('#connect').click()
    page.set_viewport_size({'width': 740, 'height': 634})
    page.wait_for_function("document.querySelector('#resource-cpu').textContent==='7.5 %'")
    assert page.locator('#resource-memory').inner_text() == '2.0 / 8.0 G'
    assert page.locator('#resource-disk').inner_text() == '12.0 / 60.0 G'
    page.wait_for_function("document.querySelector('#environment-rating-result').textContent==='低风险 · 22/100'")
    assert 'VPS用户：workspace' in page.locator('.environment-rating').get_attribute('title')
    labels = page.locator('.ip-address-label')
    assert abs(labels.nth(0).bounding_box()['x'] - labels.nth(1).bounding_box()['x']) < 1
    assert abs(labels.nth(0).bounding_box()['x'] - page.locator('.server-resources dt').first.bounding_box()['x']) < 1
    page.screenshot(path=str(evidence / 'workspace.png'))
    page.evaluate("environmentMode='fail'")
    page.clock.fast_forward(60100)
    page.wait_for_function("document.querySelector('#environment-rating-result').textContent==='暂不可用'")
    page.evaluate("environmentMode='hold'")
    page.clock.fast_forward(60100)
    page.wait_for_function('window.heldEnvironment')
    page.evaluate("resourceMode='fail'")
    page.clock.fast_forward(5100)
    page.wait_for_function("document.querySelector('#resource-cpu').textContent==='—'")
    assert '暂不可用' in page.locator('#server-resources').get_attribute('title')
    page.evaluate("resourceMode='hold'")
    page.clock.fast_forward(5100)
    page.wait_for_function('window.heldResource')
    page.evaluate("showScreen('login')")
    page.locator('#advanced-connect').click()
    page.locator('#enter-preview').click()
    page.wait_for_selector('#developer-preview')
    page.evaluate('safeCodeNativeResponse({id:heldResource.id,result:{cpuPercent:99,memoryUsedBytes:1,memoryTotalBytes:2,diskUsedBytes:3,diskTotalBytes:4}})')
    page.evaluate('safeCodeNativeResponse({id:heldEnvironment.id,result:{riskScore:94,level:"high",message:"高风险"}})')
    assert page.locator('#resource-cpu').inner_text() == '—'
    assert page.locator('#environment-rating-result').inner_text() == '未连接'
    before = page.evaluate("calls.filter(m=>m.op==='resources').length")
    environment_before = page.evaluate("calls.filter(m=>m.op==='environment.assess').length")
    page.clock.fast_forward(65000)
    assert page.evaluate("calls.filter(m=>m.op==='resources').length") == before
    assert page.evaluate("calls.filter(m=>m.op==='environment.assess').length") == environment_before
    assert not errors, errors
    browser.close()
print('ADVANCED_EMPTY_PREVIEW_PORT_VALIDATION_RESOURCES_FAILURE_AND_STALE_RESPONSE_OK')
