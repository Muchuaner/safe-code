#!/usr/bin/env python3
"""真实 SSH 通道验证：安装队列被占用时仍能浏览目录，慢请求可取消。"""
import base64,hashlib,json,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko
root=Path(__file__).resolve().parent.parent;key=paramiko.RSAKey.generate(2048)
class Stub(paramiko.ServerInterface):
    def check_auth_password(self,u,p):return paramiko.AUTH_SUCCESSFUL
    def get_allowed_auths(self,u):return 'password'
    def check_channel_request(self,k,i):return paramiko.OPEN_SUCCEEDED
    def check_channel_exec_request(self,channel,command):
        request=json.loads(base64.b64decode(command.decode().split()[-1].strip("'")))
        def response():
            if request['path']=='/slow':time.sleep(3)
            try:channel.send(('SC_RESULT:'+json.dumps({'path':request['path'],'parent':'/','directories':[]})+'\n').encode());channel.send_exit_status(0);channel.shutdown_write();time.sleep(.1);channel.close()
            except (OSError,EOFError):pass
        threading.Thread(target=response,daemon=True).start();return True
with tempfile.TemporaryDirectory() as directory:
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
    def serve():
        sock,_=listener.accept();t=paramiko.Transport(sock);t.add_server_key(key);t.start_server(server=Stub());channels=[]
        while t.is_active():
            c=t.accept(.2)
            if c:channels.append(c)
    threading.Thread(target=serve,daemon=True).start()
    cfg=Path(directory)/'target.json';cfg.write_text(json.dumps({'directory':directory,'port':listener.getsockname()[1],'fingerprint':'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')}))
    result=subprocess.run([str(root/'.local/dotnet/dotnet'),'run','--project',str(root/'src/SafeCode.Verify'),'--','directory-fixture',str(cfg)],capture_output=True,text=True,timeout=30);print(result.stdout,end='');assert result.returncode==0,result.stderr;listener.close()
