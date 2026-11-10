#!/usr/bin/env python3
"""Local SSH/SFTP fixture for remount identity, settings and multiline errors. No VPS changes."""
import base64,json,os,re,shlex,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parent.parent
key=paramiko.RSAKey.generate(2048)
requests=[]
class Files(paramiko.SFTPServerInterface):
    def stat(self,path):return paramiko.SFTPAttributes.from_stat(os.stat(path))
    def lstat(self,path):return paramiko.SFTPAttributes.from_stat(os.lstat(path))
    def open(self,path,flags,attr):
        try:
            fd=os.open(path,flags,0o600);f=os.fdopen(fd,'r+b' if flags&os.O_WRONLY or flags&os.O_RDWR else 'rb')
            h=paramiko.SFTPHandle(flags);h.readfile=f;h.writefile=f;return h
        except OSError as e:return paramiko.SFTPServer.convert_errno(e.errno)
    def chattr(self,path,attr):paramiko.SFTPServer.set_file_attr(path,attr);return paramiko.SFTP_OK
    def remove(self,path):
        try:os.unlink(path);return paramiko.SFTP_OK
        except OSError as e:return paramiko.SFTPServer.convert_errno(e.errno)
class Server(paramiko.ServerInterface):
    def check_auth_password(self,u,p):return paramiko.AUTH_SUCCESSFUL
    def get_allowed_auths(self,u):return 'password'
    def check_channel_request(self,k,i):return paramiko.OPEN_SUCCEEDED
    def check_port_forward_request(self,address,port):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listeners.append(listener);return listener.getsockname()[1]
    def check_channel_exec_request(self,channel,command):
        request=json.loads(Path(shlex.split(command.decode())[-1]).read_text());requests.append(request)
        def reply():
            op=request['op'];options=request.get('options') or {};status=0
            if op=='fixture_error':
                channel.send_stderr(b'legacy stderr detail\n')
                line='SC_ERROR_JSON:'+json.dumps('挂载失败\n第二行原因\n最后一行',ensure_ascii=False);status=1
            elif any(m['remote'].endswith('/nonempty') for m in options.get('mappings',[])):
                line='SC_ERROR_JSON:'+json.dumps('服务器目录不是空目录，请选择新的或空的目录。');status=1
            elif not re.fullmatch('[a-f0-9]{20}',options.get('clientId','')):
                line='SC_ERROR_JSON:'+json.dumps('挂载客户端标识无效。');status=1
            else:line='SC_RESULT:'+json.dumps({'clientId':options['clientId'],'paths':[m['remote'] for m in options.get('mappings',[])]})
            channel.send((line+'\n').encode());channel.send_exit_status(status);channel.shutdown_write();time.sleep(.05);channel.close()
        threading.Thread(target=reply,daemon=True).start();return True
with tempfile.TemporaryDirectory(prefix='safe-code-mount-') as directory:
    listeners=[];transports=[];channels=[];listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();stopped=threading.Event()
    def connection(sock):
        t=paramiko.Transport(sock);transports.append(t);t.add_server_key(key);t.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:t.start_server(server=Server())
        except (EOFError,OSError,paramiko.SSHException):
            if stopped.is_set():return
            raise
        while t.is_active() and not stopped.is_set():
            ch=t.accept(.1)
            if ch:channels.append(ch)
    def serve():
        while not stopped.is_set():
            try:sock,_=listener.accept()
            except OSError:return
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=serve,daemon=True).start()
    config=Path(directory)/'fixture.json';config.write_text(json.dumps({'directory':directory,'port':listener.getsockname()[1],'key':base64.b64encode(key.asbytes()).decode()}))
    dll=sys.argv[1] if len(sys.argv)>1 else str(root/'.local/mount-fix-build/bin/SafeCode.Verify/debug/SafeCode.Verify.dll')
    try:
        result=subprocess.run([str(root/'.local/dotnet/dotnet'),dll,'mount-fixture',str(config)],capture_output=True,text=True,timeout=40)
        print(result.stdout,end='');assert result.returncode==0,result.stderr
        assert [r['op'] for r in requests]==['mount_apply','mount_apply','mount_apply','mount_remove','mount_apply','mount_remove','fixture_error']
    finally:
        stopped.set();listener.close()
        for t in transports:t.close()
        for s in listeners:s.close()
