#!/usr/bin/env python3
"""Real OpenSSH parser fixture for a known_hosts path containing spaces.
No VPS, no production endpoint, no user credentials."""
import base64,shlex,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parent.parent
dll=sys.argv[1] if len(sys.argv)>1 else str(root/'src/SafeCode.Verify/bin/Debug/net10.0/SafeCode.Verify.dll')
dotnet=root/'.local/dotnet/dotnet'
key=paramiko.RSAKey.generate(2048)
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
stop=threading.Event();transports=[]
class Server(paramiko.ServerInterface):
    def check_auth_password(self,user,password):return paramiko.AUTH_FAILED
    def get_allowed_auths(self,user):return 'publickey,password'
    def check_channel_request(self,kind,channel):return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
def serve():
    while not stop.is_set():
        try:sock,_=listener.accept()
        except OSError:return
        transport=paramiko.Transport(sock);transports.append(transport);transport.add_server_key(key)
        try:transport.start_server(server=Server())
        except Exception:pass
threading.Thread(target=serve,daemon=True).start()
try:
    with tempfile.TemporaryDirectory(prefix='safe-code-ssh-space-') as directory:
        known=Path(directory)/'Application Support'/'known_hosts';known.parent.mkdir()
        body=base64.b64encode(key.asbytes()).decode()
        known.write_text(f'[127.0.0.1]:{port} ssh-rsa {body}\n')
        config=Path(directory)/'ssh-client-config';config.write_text('# isolated fixture\n')
        option=subprocess.run([str(dotnet),str(dll),'ssh-known-hosts-option',str(known)],capture_output=True,text=True,timeout=30,check=True).stdout.strip()
        assert option==f'UserKnownHostsFile="{known}"',option
        base=['/usr/bin/ssh','-v','-F',str(config),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','PreferredAuthentications=none','-o','ConnectTimeout=5','-o',option,'-p',str(port),'127.0.0.1','true']
        def check(label,command):
            result=subprocess.run(command,capture_output=True,text=True,timeout=20)
            stderr=result.stderr
            assert 'Host key verification failed' not in stderr,(label,stderr)
            assert f'Found key in {known}:1' in stderr,(label,stderr)
            assert 'Permission denied' in stderr,(label,stderr)
            print(label+' ok')
        check('windows-argv',base)
        check('mac-shell',['/bin/sh','-c',' '.join(shlex.quote(arg) for arg in base)])
        # The old unquoted value must be observably split by OpenSSH, so this fixture
        # fails for the previous implementation rather than only checking source text.
        broken=base[:]
        broken[broken.index(option)]=f'UserKnownHostsFile={known}'
        result=subprocess.run(broken,capture_output=True,text=True,timeout=20)
        assert 'Host key verification failed' in result.stderr,result.stderr
        print('unquoted-control-fails-as-expected')
finally:
    stop.set();listener.close()
    for transport in transports:
        try:transport.close()
        except Exception:pass
print('SSH_KNOWN_HOSTS_PATH_QUOTING_REAL_SSH_OK')
