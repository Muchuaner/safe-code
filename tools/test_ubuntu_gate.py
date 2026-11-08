#!/usr/bin/env python3
"""使用真实 SSH 握手验证：非 Ubuntu 在任何部署写入命令前被拒绝。"""
import base64,hashlib,json,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko
root=Path(__file__).resolve().parent.parent
key=paramiko.RSAKey.generate(2048)
commands=[]
class Stub(paramiko.ServerInterface):
    def check_auth_password(self,username,password):return paramiko.AUTH_SUCCESSFUL if password=='fixture-only' else paramiko.AUTH_FAILED
    def get_allowed_auths(self,username):return 'password'
    def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_exec_request(self,channel,command):
        commands.append(command.decode())
        def respond():
            time.sleep(.2)
            channel.send(b'ID=debian\nPRETTY_NAME="Debian GNU/Linux"\n');channel.send_exit_status(0);channel.shutdown_write();time.sleep(.2);channel.close()
        threading.Thread(target=respond,daemon=True).start();return True
with tempfile.TemporaryDirectory() as folder:
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    port=listener.getsockname()[1]
    def serve():
        sock,_=listener.accept();transport=paramiko.Transport(sock);transport.add_server_key(key);transport.start_server(server=Stub())
        channels=[]
        while transport.is_active():
            channel=transport.accept(1)
            if channel:channels.append(channel)
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    cfg=Path(folder)/'target.json';cfg.write_text(json.dumps({'host':'127.0.0.1','username':'root','password':'fixture-only','port':port,'fingerprint':'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')}));cfg.chmod(0o600)
    p=subprocess.run([str(root/'.local/dotnet/dotnet'),str(root/'src/SafeCode.Verify/bin/Debug/net10.0/SafeCode.Verify.dll'),str(cfg),'inspect'],capture_output=True,text=True,timeout=45)
    assert p.returncode==1,(p.returncode,p.stdout,p.stderr)
    assert '仅支持 Ubuntu' in p.stderr,(p.stdout,p.stderr)
    assert commands==['cat /etc/os-release'],commands
    print(json.dumps({'passed':True,'system':'Debian SSH fixture','commands':commands,'writes':0},ensure_ascii=False))
