"""Real local SSH proof of the unprivileged alias helper. No Windows activation."""
import importlib.util,json,os,subprocess,sys,tempfile,unittest,socket,threading,time
from pathlib import Path
from unittest.mock import patch
import paramiko
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class HostBridge(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name).resolve();self.client='a'*20;self.relative='.ssh/safe-code-host/'+self.client+'/identity'
    def call(self,action,success=True,**options):
        request={'operation':'host_bridge','relative':self.relative,'clientId':self.client,'action':action,'userSid':'S-1-5-21-10-20-30-1001',**options}
        p=subprocess.run([sys.executable,'-I','-c',a.USER_FILES],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(self.home)})
        if success:self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
        self.assertNotEqual(p.returncode,0);return p
    def test_prepare_never_exports_private_key_and_remove_revokes(self):
        first=self.call('prepare');self.assertTrue(first['publicKey'].startswith('ssh-ed25519 '));self.assertNotIn('PRIVATE',json.dumps(first));self.assertEqual((self.home/self.relative).stat().st_mode&0o777,0o600)
        second=self.call('prepare');self.assertNotEqual(first,second);self.call('remove');self.assertFalse((self.home/self.relative).exists())
    def test_configure_rejects_untrusted_fields_without_touching_user_config(self):
        self.call('prepare');ssh=self.home/'.ssh';(ssh/'config').write_text('Host personal\n  HostName untouched.example\n')
        for options in [{'alias':'x\nHost *','username':'tester','port':2222,'hostKey':'ssh-ed25519 AA=='},{'alias':'win','username':'tester\nroot','port':2222,'hostKey':'ssh-ed25519 AA=='},{'alias':'win','username':'tester','port':22,'hostKey':'ssh-ed25519 AA=='},{'alias':'win','username':'tester','port':2222,'hostKey':'ssh-rsa AA=='}]:self.call('configure',False,**options)
        self.assertEqual((ssh/'config').read_text(),'Host personal\n  HostName untouched.example\n');self.assertFalse((self.home/self.relative).with_name('config').exists())
    def test_public_listener_is_rejected_before_alias_write(self):
        with patch.object(a,'run',return_value=subprocess.CompletedProcess([],0,'LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*','')),patch.object(a,'user_files') as helper:
            with self.assertRaisesRegex(RuntimeError,'回环'):a.host_bridge({'clientId':self.client,'action':'configure','port':2222})
            helper.assert_not_called()
    def test_actual_key_login_host_pinning_and_dedicated_config(self):
        key=self.call('prepare')['publicKey'];host=self.home/'fixture-host-key';subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(host)],check=True)
        server_key=paramiko.Ed25519Key.from_private_key_file(str(host));pub=host.with_name(host.name+'.pub').read_text().strip();transports=[];threads=[];stop=threading.Event()
        class Server(paramiko.ServerInterface):
            def check_auth_publickey(self,user,presented):return paramiko.AUTH_SUCCESSFUL if user=='tester' and presented.get_base64()==key.split()[1] else paramiko.AUTH_FAILED
            def get_allowed_auths(self,user):return 'publickey'
            def check_channel_request(self,kind,channel):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self,ch,command):
                def send():ch.send(b'fixture\\tester\n' if command==b'whoami' else b'S-1-5-21-10-20-30-1001\n');ch.send_exit_status(0);ch.shutdown_write();time.sleep(.1);ch.close()
                self.command=command;threading.Thread(target=send,daemon=True).start();return command==b'whoami' or command.startswith(b'powershell.exe ')
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.2);port=listener.getsockname()[1]
        def serve():
            while not stop.is_set():
                try:conn,_=listener.accept()
                except socket.timeout:continue
                except OSError:return
                def handle(conn=conn):
                    transport=paramiko.Transport(conn);transports.append(transport);transport.add_server_key(server_key)
                    try:
                        transport.start_server(server=Server());channels=[]
                        while transport.is_active() and not stop.is_set():
                            channel=transport.accept(.1)
                            if channel:channels.append(channel)
                    except (EOFError,OSError,paramiko.SSHException):pass
                thread=threading.Thread(target=handle,daemon=True);thread.start();threads.append(thread)
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        try:
            result=self.call('configure',alias='win',username='tester',port=port,hostKey=pub);self.assertTrue(result['verified']);self.assertIn('ssh -F ',result['command']);self.assertTrue(result['command'].endswith(' win'))
            config=(self.home/self.relative).with_name('config').read_text();self.assertIn('StrictHostKeyChecking yes',config);self.assertIn('ForwardAgent no',config);self.assertFalse((self.home/'.ssh/config').exists())
            self.call('configure',False,alias='win',username='tester',port=port,hostKey=pub,userSid='S-1-5-21-10-20-30-9999')
            bad=pub.split();bad[1]=key.split()[1]
            self.call('configure',False,alias='win',username='tester',port=port,hostKey=' '.join(bad))
        finally:
            stop.set();listener.close()
            for transport in transports:transport.close()
            thread.join(2)
    def test_symlink_escape_is_rejected(self):
        outside=tempfile.TemporaryDirectory();self.addCleanup(outside.cleanup);(self.home/'.ssh').symlink_to(Path(outside.name).resolve())
        self.call('prepare',False)
if __name__=='__main__':unittest.main()
