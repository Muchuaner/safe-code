"""Exercise the shipped frontend companion in a real headless browser."""
import importlib.util,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Refresh(unittest.TestCase):
    def test_native_refresh_keeps_shell_open_and_retries_after_disconnect(self):
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.evaluate('''() => {
              window.fixture={revision:'one',requests:0,hidden:false,events:0};
              window.WebSocket=class extends EventTarget {static OPEN=1;constructor(url){super();this.url=url;this.readyState=1;}};
              window.setInterval=fn=>fixture.check=fn;
              window.fetch=async()=>{fixture.requests++;return {ok:true,json:async()=>({revision:fixture.revision})};};
              Object.defineProperty(document,'hidden',{get:()=>fixture.hidden});
            }''')
            page.evaluate(a.CLOUD_MOUNT_REFRESH)
            page.evaluate('''async () => {
              const assert=(value,message)=>{if(!value)throw Error(message);};
              const chat=new WebSocket('ws://localhost/ws?token=private'),shell=new WebSocket('ws://localhost/shell');
              chat.addEventListener('message',event=>{assert(JSON.parse(event.data).kind==='websocket_reconnected','native event');fixture.events++;});
              shell.addEventListener('message',()=>{throw Error('terminal was touched');});
              fixture.revision='two';await fixture.check();assert(fixture.events===1,'refresh on revision change');await fixture.check();assert(fixture.events===1,'no repeated refresh');
              fixture.hidden=true;fixture.revision='three';const before=fixture.requests;await fixture.check();assert(fixture.requests===before,'pause while hidden');
              fixture.hidden=false;document.dispatchEvent(new Event('visibilitychange'));await new Promise(resolve=>queueMicrotask(()=>queueMicrotask(resolve)));assert(fixture.events===2,'refresh when visible');
              chat.readyState=3;chat.dispatchEvent(new Event('close'));fixture.revision='four';await fixture.check();assert(fixture.events===2,'closed socket');
              const next=new WebSocket('ws://localhost/ws');next.addEventListener('message',()=>fixture.events++);await fixture.check();assert(fixture.events===3,'retry when socket returns');
              assert(shell.readyState===1,'shell remained open');assert(WebSocket.OPEN===1,'native constants');
            }''')
            self.assertEqual(errors,[]);browser.close()
if __name__=='__main__':unittest.main()
