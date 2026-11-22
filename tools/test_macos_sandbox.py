"""Offline native backend verification. All state and keys are disposable fixtures; no server access."""
import base64,json,os,queue,secrets,subprocess,tempfile,threading,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='safe-code-sandbox-') as d:
 p=subprocess.Popen([os.environ.get('SAFECODE_DOTNET', str(root/'.local/dotnet/dotnet')),str(root/'src/SafeCode.MacBackend/bin/Debug/net10.0/SafeCode.MacBackend.dll'),'--data-directory',d],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 q=queue.Queue()
 def read():
  for line in p.stdout:q.put(json.loads(line))
 threading.Thread(target=read,daemon=True).start()
 p.stdin.write(json.dumps({'key':base64.b64encode(secrets.token_bytes(32)).decode(),'offline':True})+'\n');p.stdin.flush()
 count=0
 def rpc(op,args={}):
  global count
  count+=1;id=str(count);p.stdin.write(json.dumps({'id':id,'op':op,'args':args})+'\n');p.stdin.flush();end=time.monotonic()+10
  while time.monotonic()<end:
   message=q.get(timeout=max(.01,end-time.monotonic()))
   if message.get('id')==id:return message
  raise TimeoutError(op)
 try:
  for op in ['connect','connection.reauthenticate','deploy','mount.apply','host.enable','bridge.start','desktopKey.enable','safeCodeApi.install','safeCodeApi.enablePublic','claudeDesktop.start','claudeDesktop.stop','cloud.open','ip.apply','terminal','desktop','feedback.send','updates.download','updates.install','ip.info']:
   reply=rpc(op,{'host':'production.invalid','username':'fixture','password':'fixture-only'});assert '隔离预览' in reply['error'],(op,reply)
  prefs=rpc('services.preferences')['result']
  assert not prefs['enabled'] and not prefs['diagnosticsEnabled'] and not prefs['serviceAvailable']
  assert rpc('services.consent',{'enabled':True})['result']['enabled']
  assert rpc('diagnostics.consent',{'enabled':True})['result']['diagnosticsEnabled']
  assert not rpc('services.consent',{'enabled':False})['result']['enabled']
  assert not rpc('diagnostics.consent',{'enabled':False})['result']['diagnosticsEnabled']
  prefs=rpc('services.confirm')['result']
  assert prefs['confirmed'] and not prefs['enabled'] and not prefs['diagnosticsEnabled']
  assert rpc('login.load')['result'] is None;assert rpc('mount.status')['result']['count']==0
  assert not (Path(d)/'known_hosts').exists();assert not (Path(d)/'login.aes').exists();assert rpc('app.shutdown')['result'] is True
 finally:
  p.stdin.close();p.wait(timeout=10)
 assert p.returncode==0,p.stderr.read()
print('OFFLINE_SANDBOX_BLOCKS_NETWORK_AND_MUTATION_OPERATIONS_WITH_DISPOSABLE_STATE')
