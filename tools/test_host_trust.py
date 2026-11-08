#!/usr/bin/env python3
"""真实 SSH 握手轮换服务器密钥；确认前不得密码认证，确认后只读 Debian 检查。"""
import base64,hashlib,json,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko
root=Path(__file__).resolve().parent.parent
keys=[paramiko.RSAKey.generate(2048),paramiko.RSAKey.generate(2048)];auths=[];commands=[]
class Stub(paramiko.ServerInterface):
    def check_auth_password(self,username,password):auths.append(username);return paramiko.AUTH_SUCCESSFUL
    def get_allowed_auths(self,username):return 'password'
    def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED
    def check_channel_exec_request(self,channel,command):
        commands.append(command.decode())
        def answer():
            time.sleep(.1);channel.send(b'ID=debian\n');channel.send_exit_status(0);channel.shutdown_write();time.sleep(.1);channel.close()
        threading.Thread(target=answer,daemon=True).start();return True
with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder);mode=folder/'mode';mode.write_text('first');listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();running=True
    def connection(sock):
        t=paramiko.Transport(sock);t.add_server_key(keys[mode.read_text()=='second'])
        try:
            t.start_server(server=Stub());channels=[]
            while t.is_active():
                c=t.accept(.5)
                if c:channels.append(c)
        except (EOFError,paramiko.SSHException):pass
        finally:t.close()
    def serve():
        while running:
            try:sock,_=listener.accept()
            except OSError:return
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=serve,daemon=True).start()
    fp=lambda k:'SHA256:'+base64.b64encode(hashlib.sha256(k.asbytes()).digest()).decode().rstrip('=')
    cfg=folder/'config.json';cfg.write_text(json.dumps(dict(directory=str(folder/'client'),port=listener.getsockname()[1],mode=str(mode),first=fp(keys[0]),second=fp(keys[1]))))
    r=subprocess.run([str(root/'.local/dotnet/dotnet'),'run','--project',str(root/'src/SafeCode.Verify'),'--','trust-fixture',str(cfg)],capture_output=True,text=True,timeout=60)
    print(r.stdout,end='');assert r.returncode==0,r.stderr
    assert len(auths)==2,auths;assert commands==['cat /etc/os-release']*2,commands
    print('UNTRUSTED_HANDSHAKES_AUTHENTICATED_ZERO_TIMES; APPROVED_READ_ONLY_CHECKS=2');running=False;listener.close()
