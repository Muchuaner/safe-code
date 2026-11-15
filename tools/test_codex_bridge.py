"""Actual normal-user installer/revoker in a temporary home; no model calls."""
import base64,hashlib,importlib.util,json,os,socket,struct,subprocess,sys,tempfile,threading,unittest,zipfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class CodexBridge(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix="scb-",dir="/tmp");self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name).resolve();self.state=self.home/'.local/state/safe-code/codex-bridge'
  body=base64.b64encode(bytes(range(96))).decode();self.key='ssh-rsa '+body+' fixture';self.key_id=hashlib.sha256(body.encode()).hexdigest()[:20];self.client='c_'+'b'*24
  with zipfile.ZipFile(Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/codex_bridge.zip') as z:self.files={name:base64.b64encode(z.read(name)).decode() for name in z.namelist()}
  self.claude=self.home/'claude-fixture';self.claude.write_text("#!/bin/sh\nprintf '%s\\n' '--tools --strict-mcp-config'\n");self.claude.chmod(0o700)
  (self.home/'.ssh').mkdir();self.auth=self.home/'.ssh/authorized_keys';self.original='ssh-ed25519 '+base64.b64encode(b'untouched'*12).decode()+' personal\n';self.auth.write_text(self.original)
 def call(self,action='install',success=True,**extra):
  request={'operation':'codex_bridge','relative':'.local/state/safe-code/codex-bridge','action':action,'publicKey':self.key,'keyId':self.key_id,'clientId':self.client,'files':self.files,'sourceHash':a.CODEX_BRIDGE_SOURCE_HASH,'node':sys.executable,'claude':str(self.claude),'username':'fixture',**extra}
  p=subprocess.run([sys.executable,'-I','-c',a.USER_FILES],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(self.home)})
  if success:self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
  self.assertNotEqual(p.returncode,0);return p
 def test_embedded_source_bundle_matches_reviewed_vendor_files(self):
  root=Path(__file__).resolve().parents[1];payload=root/'src/SafeCode.Core/Remote/codex_bridge.zip'
  self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(),a.CODEX_BRIDGE_SOURCE_HASH)
  with zipfile.ZipFile(payload) as archive:
   for name in archive.namelist():self.assertEqual(archive.read(name),(root/'third_party/claude-codex-vps'/name).read_bytes())
 def test_idempotent_restricted_key_and_existing_authorizations(self):
  result=self.call();self.assertTrue(result['ready']);first=self.auth.read_text();self.call();self.assertEqual(first,self.auth.read_text());self.assertIn(self.original,first);self.assertIn('restrict,command=',first);self.assertEqual(first.count(self.key.split()[1]),1)
  env={**os.environ,'SSH_ORIGINAL_COMMAND':'ccw-ping'};p=subprocess.run([str(self.state/'bin/connect')],capture_output=True,text=True,env=env);self.assertEqual(p.stdout.strip(),'codex-claude-win-ok')
  self.call('remove');self.assertEqual(self.auth.read_text(),self.original);self.assertFalse((self.state/'clients'/(self.key_id+'.json')).exists())
 def test_unknown_state_and_external_symlink_are_preserved(self):
  self.state.mkdir(parents=True);(self.state/'user-file').write_text('keep');self.call(success=False);self.assertEqual((self.state/'user-file').read_text(),'keep');self.assertEqual(self.auth.read_text(),self.original)
 def test_broad_existing_key_is_never_accepted_as_restricted(self):
  self.auth.write_text(self.original+self.key+'\n');self.call(success=False);self.assertNotIn('restrict,command=',self.auth.read_text());self.call('remove');self.assertIn(self.key,self.auth.read_text())
 def test_source_edit_is_not_overwritten(self):
  self.call();source=self.home/'.local/share/safe-code/codex-bridge'/a.CODEX_BRIDGE_SOURCE_HASH[:16]/'src/vps/daemon.mjs';source.write_text('user modified')
  self.call(success=False);self.assertEqual(source.read_text(),'user modified')
 def test_cancel_only_the_bound_client_without_starting_a_turn(self):
  self.call();run=self.state/'run';run.mkdir();server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(run/'daemon.sock'));server.listen();events=[];errors=[]
  def read_n(conn,n):
   data=b''
   while len(data)<n:data+=conn.recv(n-len(data))
   return data
  def read(conn):
   head,body=struct.unpack('>II',read_n(conn,8));h=json.loads(read_n(conn,head));read_n(conn,body);return h
  def send(conn,h):
   data=json.dumps(h).encode();conn.sendall(struct.pack('>II',len(data),0)+data)
  def serve():
   try:
    conn,_=server.accept()
    with conn:
     events.append(read(conn));send(conn,{'op':'hello.ok','turns':[{'turnId':'turn_owned','exited':False}]});events.append(read(conn));send(conn,{'op':'turn.exit','turnId':'turn_owned'})
   except Exception as e:errors.append(e)
  thread=threading.Thread(target=serve,daemon=True);thread.start()
  try:result=self.call('remove');self.assertIsNone(result['warning']);thread.join(3);self.assertFalse(errors);self.assertEqual([e['op'] for e in events],['hello','turn.cancel']);self.assertEqual(events[0]['clientId'],self.client);self.assertEqual(events[1]['turnId'],'turn_owned');self.assertEqual(self.auth.read_text(),self.original)
  finally:server.close()
 def test_auth_symlink_outside_home_is_rejected(self):
  outside=tempfile.TemporaryDirectory();self.addCleanup(outside.cleanup);file=Path(outside.name)/'keys';file.write_text('keep');self.auth.unlink();self.auth.symlink_to(file);self.call(success=False);self.assertEqual(file.read_text(),'keep')
 def test_remove_absent_component_does_not_create_state(self):
  self.call('remove');self.assertFalse(self.state.exists());self.assertEqual(self.auth.read_text(),self.original)
if __name__=='__main__':unittest.main()
