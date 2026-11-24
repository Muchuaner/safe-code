"""Exercise the saved-key picker with isolated native responses."""
import argparse
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--cdp")
parser.add_argument("--screenshots", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
with sync_playwright() as pw:
    browser = (pw.chromium.connect_over_cdp(args.cdp) if args.cdp
               else pw.chromium.launch(channel="chrome", headless=True))
    context = browser.new_context(viewport={"width": 800, "height": 595}, device_scale_factor=1.5)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""
        window.__safeCodeDesktop=true;
        let keys=[{id:'first',name:'测试网关'}], current='first';
        window.launches=0;let pending=null, authorized=new Set(['first']);
        window.chrome={webview:{postMessage({op,args,id}){
          let result=null,error=null;
          const status=()=>({configured:true,running:true,deviceAlias:'本机设备',startedAt:Date.now()-720000});
          switch(op){
            case 'services.preferences':case 'services.confirm':result={};break;
            case 'desktopKey.mode':result=true;break;
            case 'notice.state':result={accepted:true};break;
            case 'desktopKey.status':result=status();break;
            case 'desktopKey.balance':result={usedNanos:1200000000,remainingNanos:98800000000,models:[]};break;
            case 'desktopKey.list':result=keys.map(k=>({...k,running:k.id===current,current:k.id===current}));break;
            case 'desktopKey.save':keys.push({id:'second',name:args.name});result=true;break;
            case 'desktopKey.savedBalance':result={remainingNanos:args.id==='first'?98800000000:null};break;
            case 'desktopKey.enable':
              if(window.failSwitch)error='目标网关暂不可用';
              else if(!authorized.has(args.savedId)){pending=args.savedId;result={requiresDevice:true};}
              else {current=args.savedId;result=status();}
              break;
            case 'desktopKey.cancelDevice':pending=null;break;
            case 'desktopKey.authorizeDevice':
              if(!args.riskAccepted || !pending)error='缺少设备授权';
              else {authorized.add(pending);current=pending;pending=null;result=status();}
              break;
            case 'claudeDesktop.reopen':window.launches++;break;
          }
          queueMicrotask(()=>window.safeCodeNativeResponse({id,result,error}));
        }}};
    """)
    def assets(route):
        resource = root / route.request.url.split("safe-code.local/", 1)[-1].split("?", 1)[0]
        if resource.is_file():
            route.fulfill(path=str(resource))
        else:
            route.fulfill(status=404, body="")
    page.route("**/*", assets)
    page.goto("https://safe-code.local/web/index.html")
    picker = page.locator("#api-switch-key")
    expect(picker).to_be_visible()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(args.screenshots / "api-access.png"))
    picker.click()
    page.locator("#key-add").click()
    page.locator("#key-label").fill("VPS 环境")
    page.locator("#key-code").fill("fixture")
    page.get_by_role("button", name="保存 Key", exact=True).click()
    rows = page.locator(".key-row")
    expect(rows.nth(0)).to_contain_text("余额 $98.80")
    expect(rows.nth(1)).to_contain_text("不限额")
    expect(rows.nth(0)).to_contain_text("正在使用")
    if args.screenshots:
        page.screenshot(path=str(args.screenshots / "key-list.png"))
    page.evaluate("window.failSwitch=true")
    page.get_by_role("button", name="切换到 VPS 环境").click()
    expect(page.locator("#key-list-status")).to_have_text("目标网关暂不可用")
    assert page.evaluate("launches===0")
    page.evaluate("window.failSwitch=false")
    page.get_by_role("button", name="切换到 VPS 环境").click()
    expect(page.locator("#host-dialog")).to_be_visible()
    page.locator("#host-close").click()
    expect(rows.nth(0)).to_contain_text("正在使用")
    page.get_by_role("button", name="切换到 VPS 环境").click()
    page.locator("#host-risk").check()
    page.locator("#host-enable").click()
    expect(page.locator("#key-list-dialog")).not_to_be_visible()
    picker.click()
    expect(rows.nth(1)).to_contain_text("正在使用")
    page.get_by_role("button", name="切换到 测试网关").click()
    page.wait_for_function("launches===2")
    assert not errors, errors
    context.close()
    if not args.cdp:
        browser.close()
    print("Saved-key picker: add, balances, failure, switch, return, launch passed")
