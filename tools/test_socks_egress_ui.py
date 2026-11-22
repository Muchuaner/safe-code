#!/usr/bin/env python3
"""Verify both IP card actions and the SOCKS5 UI using an offline native bridge fixture."""
from pathlib import Path
import os
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent.parent
output = Path(os.environ.get('SAFECODE_SOCKS_EVIDENCE', root / '.local/evidence/socks-egress'))
output.mkdir(exist_ok=True, parents=True)
origin = 'https://safe-code.local/'
platform = os.environ.get('SAFECODE_TEST_PLATFORM', 'macos')
bridge = '''
window.__safeCodeDesktop=true;
window.__safeCodePlatform=PLATFORM;
window.socksOperations=[];
const server={
  osId:'ubuntu',osVersion:'24.04',assignedIps:['9.9.9.9','1.1.1.1'],observedEgressIp:'192.0.2.10',
  statuses:{},socksEgress:{enabled:false}
};
window.chrome={webview:{postMessage({id,op,args}){
  let result=null;
  if(op==='services.preferences')result={enabled:false,diagnosticsEnabled:false,confirmed:true};
  if(op==='notice.state')result={accepted:false};
  if(op==='desktopKey.status')result={configured:false,running:false};
  if(op==='connect')result={ready:true,server};
  if(op==='inspect')result=server;
  if(op==='socks.status')result=server.socksEgress;
  if(op.startsWith('socks.'))window.socksOperations.push(op);
  if(op==='socks.probe'){
    if(window.failUdp){
      result={tcp:true,udp:false,observedIp:'198.18.46.1',udpMessage:'等待代理转发超时。'};
    }else{
      result={tcp:true,udp:true,observedIp:'198.18.46.1'};
    }
  }
  if(op==='socks.enable'){
    if(!args.accepted)throw Error('Missing confirmation');
    server.socksEgress={enabled:true,guardActive:true,tunnelRunning:true,endpoint:'198.18.45.1:1080',observedIp:'198.18.46.1'};
    server.observedEgressIp=server.socksEgress.observedIp;
    result=server;
  }
  if(op==='socks.disable'){
    server.socksEgress={enabled:false};server.observedEgressIp='192.0.2.10';
    result=server;
  }
  queueMicrotask(()=>window.safeCodeNativeResponse({id,result}));
}}};
'''
with sync_playwright() as playwright:
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    browser = playwright.chromium.launch(executable_path=str(chrome) if chrome.is_file() else None, headless=True)
    page = browser.new_page(viewport={'width': 900, 'height': 730}, device_scale_factor=2)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(request):
        url = request.request.url
        if not url.startswith(origin):
            request.abort()
            return
        file = root / url[len(origin):].split('?', 1)[0]
        if file.is_file():
            request.fulfill(path=str(file))
        else:
            request.fulfill(status=404)

    page.route('**/*', route)
    page.add_init_script(bridge.replace('PLATFORM', repr(platform), 1))
    page.goto(origin + 'web/index.html')
    page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#accept').click()
    page.locator('#ip').fill('192.0.2.10')
    page.locator('#username').fill('fixture')
    page.locator('#password').fill('fixture-only')
    page.locator('#connect').click()
    page.locator('#maintenance-mode').click()
    card = page.locator('.advanced-card').filter(has=page.locator('#change-ips'))
    assert card.locator('button').all_text_contents() == ['更换IP', '开发模式']
    card.screenshot(path=str(output / 'ip-card.png'))

    page.locator('#change-ips').click()
    expect(page.locator('#ip-policy-dialog')).to_be_visible()
    expect(page.locator('#lock-egress')).to_be_visible()
    page.locator('#close-ip-policy').click()

    page.locator('#socks-experimental').click()
    dialog = page.locator('#socks-dialog')
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text('开发功能，谨慎使用')).to_be_visible()
    expect(page.locator('#socks-enable')).to_be_disabled()
    expect(page.locator('#socks-test')).to_be_disabled()
    field = page.locator('#socks-url')
    expect(field).to_be_enabled()
    expect(field).to_have_attribute('type', 'password')
    field.fill('socks5://fixture-user:fixture-password@198.18.45.1:1080')

    page.evaluate('window.failUdp=true')
    page.locator('#socks-test').click()
    expect(page.locator('#socks-tcp')).to_have_text('通过')
    expect(page.locator('#socks-udp')).to_have_text('未通过')
    expect(page.locator('#socks-enable')).to_be_disabled()

    page.evaluate('window.failUdp=false')
    page.locator('#socks-test').click()
    expect(page.locator('#socks-udp')).to_have_text('通过')
    expect(page.locator('#socks-observed')).to_have_text('198.18.46.1')
    expect(page.locator('#socks-enable')).to_be_disabled()
    page.locator('#socks-confirm').check()
    field.fill(field.input_value() + '/')
    expect(page.locator('#socks-enable')).to_be_disabled()
    expect(page.locator('#socks-udp')).to_have_text('待检测')
    page.locator('#socks-test').click()
    page.locator('#socks-confirm').check()
    expect(page.locator('#socks-enable')).to_be_enabled()
    dialog.screenshot(path=str(output / 'socks5-detected.png'))

    page.locator('#socks-enable').click()
    expect(page.locator('#socks-disable')).to_be_visible()
    expect(page.locator('#ip-policy-state')).to_have_text('SOCKS5 开发模式')
    expect(page.locator('#server-address')).to_have_text('198.18.46.1')
    expect(field).to_have_value('')
    dialog.screenshot(path=str(output / 'socks5-enabled.png'))

    page.locator('#socks-disable').click()
    expect(page.locator('#socks-enable')).to_be_disabled()
    expect(page.locator('#server-address')).to_have_text('192.0.2.10')
    page.locator('#socks-close').click()
    page.locator('#socks-experimental').click()
    expect(field).to_have_value('')
    assert page.evaluate('window.socksOperations') == [
        'socks.status', 'socks.probe', 'socks.probe', 'socks.probe', 'socks.enable', 'socks.disable', 'socks.status',
    ]

    page.reload()
    page.wait_for_function('window.safeCodeNativeReady')
    page.locator('#accept').click()
    page.locator('#ip').fill('127.0.0.1')
    page.locator('#connect').click()
    page.wait_for_selector('#developer-preview')
    page.locator('#maintenance-mode').click()
    page.locator('#socks-experimental').click()
    expect(field).to_be_disabled()
    expect(page.locator('#socks-status')).to_contain_text('连接 Ubuntu 服务器')
    assert not page.evaluate('window.socksOperations')
    assert not errors, errors
    browser.close()

print('SOCKS5_CARD_PROBE_CONFIRM_ENABLE_DISABLE_OFFLINE_PREVIEW_PASSED')
