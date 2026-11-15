"""真实握手：服务器新增高优先级密钥后仍使用原信任密钥，替换密钥则拒绝认证。"""
import json,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parents[1]
original=paramiko.RSAKey.generate(2048)
replacement=paramiko.RSAKey.generate(2048)
additional=paramiko.ECDSAKey.generate()
auths=[]
class Server(paramiko.ServerInterface):
    def check_auth_password(self,user,password):
        auths.append(mode.read_text());return paramiko.AUTH_SUCCESSFUL
    def get_allowed_auths(self,user):return 'password'
    def check_channel_request(self,kind,channel):return paramiko.OPEN_SUCCEEDED
    def check_channel_exec_request(self,channel,command):
        assert command.decode()=="python3 -u '/tmp/fixture/agent.py' --claude-download",command
        def answer():
            time.sleep(.05)
            channel.send(b'SC_RESULT:{"claudePayload":"/tmp/fixture/claude-payload-fixture","claudeSha256":"fixture","claudeVersion":"2.1.311"}\n')
            channel.send_exit_status(0);channel.shutdown_write();time.sleep(.05);channel.close()
        threading.Thread(target=answer,daemon=True).start();return True
class Files(paramiko.SFTPServerInterface):pass
with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder);mode=folder/'mode';mode.write_text('original')
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();transports=[]
    def connection(sock):
        transport=paramiko.Transport(sock);transports.append(transport)
        phase=mode.read_text();transport.add_server_key(replacement if phase=='changed' else original)
        if phase!='original':transport.add_server_key(additional)
        transport.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:transport.start_server(server=Server())
        except (EOFError,paramiko.SSHException):transport.close()
    def serve():
        while True:
            try:sock,_=listener.accept()
            except OSError:return
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=serve,daemon=True).start()
    cfg=folder/'config.json';cfg.write_text(json.dumps(dict(directory=str(folder/'client'),mode=str(mode),port=listener.getsockname()[1])))
    try:
        subprocess.run([str(root/'.local/dotnet/dotnet'),'run','--project',str(root/'src/SafeCode.Verify'),'--','algorithm-fixture',str(cfg)],check=True,timeout=60)
        assert auths==['original','expanded','expanded'],auths
        print('REPLACED_HOST_KEY_AUTHENTICATED_ZERO_TIMES')
    finally:
        listener.close()
        for transport in transports:transport.close()
