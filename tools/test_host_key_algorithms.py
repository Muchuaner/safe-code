"""真實握手：伺服器新增高優先級密鑰後仍使用原信任密鑰，替換密鑰則拒絕認證。"""
import json,os,re,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parents[1]
original=paramiko.RSAKey.generate(2048)
replacement=paramiko.RSAKey.generate(2048)
additional=paramiko.ECDSAKey.generate()
auths=[]
jobs=[]
polls=[]
uploads=[]
class Server(paramiko.ServerInterface):
    def __init__(self,phase):self.phase=phase
    def check_auth_password(self,user,password):
        auths.append(self.phase);return paramiko.AUTH_SUCCESSFUL
    def get_allowed_auths(self,user):return 'password'
    def check_channel_request(self,kind,channel):return paramiko.OPEN_SUCCEEDED
    def check_channel_exec_request(self,channel,command):
        command=command.decode()
        request=re.fullmatch(r"python3 -u '/tmp/fixture/agent\.py' '(/tmp/fixture/request-[a-f0-9]{32}\.json)'",command)
        poll=re.fullmatch(r"python3 -u '/tmp/fixture/agent\.py' --poll '([a-f0-9]{32})' 0",command)
        if request:
            value=json.loads((folder/Path(request[1]).name).read_text())
            assert value=={'op':'claude','options':None,'background':True},value
            job_id=f'{len(jobs)+1:032x}'
            jobs.append(job_id)
            reply={'jobId':job_id,'operation':'claude'}
        elif poll:
            job_id=poll[1]
            assert job_id in jobs and job_id not in polls,command
            polls.append(job_id)
            reply={'lines':[],'nextOffset':0,'done':True}
            if job_id==jobs[0]:
                reply['value']={'architecture':'x86_64','statuses':{'claude':True}}
            else:
                reply.update(error='VPS 下載失敗',errorCode='dns_failure')
        else:
            raise AssertionError(command)
        payload=('SC_RESULT:'+json.dumps(reply,ensure_ascii=False)+'\n').encode()
        def answer():
            time.sleep(.05)
            channel.send(payload)
            channel.send_exit_status(0);channel.shutdown_write();time.sleep(.05);channel.close()
        threading.Thread(target=answer,daemon=True).start();return True
class Files(paramiko.SFTPServerInterface):
    def local_path(self,path):
        assert re.fullmatch(r'/tmp/fixture/request-[a-f0-9]{32}\.json',path),path
        return folder/Path(path).name
    def open(self,path,flags,attr):
        uploads.append(path)
        file=os.fdopen(os.open(self.local_path(path),flags,0o600),'wb')
        handle=paramiko.SFTPHandle(flags)
        handle.writefile=file
        return handle
    def chattr(self,path,attr):
        paramiko.SFTPServer.set_file_attr(str(self.local_path(path)),attr)
        return paramiko.SFTP_OK
    def lstat(self,path):
        return paramiko.SFTPAttributes.from_stat(self.local_path(path).lstat())
    def remove(self,path):
        self.local_path(path).unlink(missing_ok=True)
        return paramiko.SFTP_OK
with tempfile.TemporaryDirectory() as folder:
    folder=Path(folder);mode=folder/'mode';mode.write_text('original')
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();transports=[]
    def connection(sock):
        transport=paramiko.Transport(sock);transports.append(transport)
        phase=mode.read_text();transport.add_server_key(replacement if phase=='changed' else original)
        if phase!='original':transport.add_server_key(additional)
        transport.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:transport.start_server(server=Server(phase))
        except (EOFError,paramiko.SSHException):transport.close()
    def serve():
        while True:
            try:sock,_=listener.accept()
            except OSError:return
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=serve,daemon=True).start()
    cfg=folder/'config.json';cfg.write_text(json.dumps(dict(directory=str(folder/'client'),mode=str(mode),port=listener.getsockname()[1])))
    try:
        subprocess.run([str(root/'.local/dotnet/dotnet'),'run','--artifacts-path',str(folder/'artifacts'),
            '--project',str(root/'src/SafeCode.Verify'),'--','algorithm-fixture',str(cfg)],check=True,timeout=60)
        assert auths[0]=='original' and auths.count('expanded')>=4 and 'changed' not in auths,auths
        assert polls==jobs and len(jobs)==len(uploads)==2,(jobs,polls,uploads)
        print('REPLACED_HOST_KEY_AUTHENTICATED_ZERO_TIMES')
    finally:
        listener.close()
        for transport in transports:transport.close()
