#!/usr/bin/env python3
"""Isolated SSH fixture: new TCP connections disappear after ip_apply, while the
already established control channel must still run the emergency rollback."""
import base64,json,os,re,shlex,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parent.parent
key=paramiko.RSAKey.generate(2048)
requests=[];control=[]
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
    def check_channel_exec_request(self,channel,command):
        text=command.decode()
        if text.startswith(('sh -c ','rm -f ')):
            control.append(text);threading.Thread(target=lambda:(channel.send_exit_status(0),channel.shutdown_write(),time.sleep(.05),channel.close()),daemon=True).start();return True
        if 'control-' in text:
            control.append(text)
            def reply_control():
                channel.send(('SC_RESULT:'+json.dumps({'rolledBack':True})+'\n').encode());channel.send_exit_status(0);channel.shutdown_write();time.sleep(.05);channel.close()
            threading.Thread(target=reply_control,daemon=True).start();return True
        path=shlex.split(text)[-1]
        try:request=json.loads(Path(path).read_text())
        except (OSError,json.JSONDecodeError):
            channel.send(b'SC_ERROR_JSON:"request missing"\n');channel.send_exit_status(1);channel.close();return True
        requests.append(request);op=request.get('op')
        if op=='ip_prepare':value={'token':'a'*24,'policy':request.get('options') or {}}
        elif op=='ip_apply':
            holder['listener'].close()
            value={'scheduled':True}
        elif op=='ip_rollback':value={'rolledBack':True}
        else:value={}
        def reply():
            channel.send(('SC_RESULT:'+json.dumps(value)+'\n').encode());channel.send_exit_status(0);channel.shutdown_write();time.sleep(.05);channel.close()
        threading.Thread(target=reply,daemon=True).start();return True
with tempfile.TemporaryDirectory(prefix='safe-code-ip-rollback-') as directory:
    holder={};transports=[];listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();holder['listener']=listener
    def connection(sock):
        transport=paramiko.Transport(sock);transports.append(transport);transport.add_server_key(key);transport.set_subsystem_handler('sftp',paramiko.SFTPServer,Files)
        try:transport.start_server(server=Server())
        except (EOFError,OSError,paramiko.SSHException):return
        while transport.is_active():
            try:channel=transport.accept(.1)
            except (EOFError,OSError):break
            if channel is None:continue
    def serve():
        while True:
            try:sock,_=listener.accept()
            except OSError:return
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=serve,daemon=True).start()
    config=Path(directory)/'fixture.json';config.write_text(json.dumps({'directory':directory,'port':listener.getsockname()[1],'key':base64.b64encode(key.asbytes()).decode()}))
    dll=sys.argv[1] if len(sys.argv)>1 else str(root/'src/SafeCode.Verify/bin/Debug/net10.0/SafeCode.Verify.dll')
    try:
        result=subprocess.run([str(root/'.local/dotnet/dotnet'),dll,'ip-rollback-fixture',str(config)],capture_output=True,text=True,timeout=40)
        print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
        assert result.returncode==0
        assert [r.get('op') for r in requests]==['ip_prepare','ip_apply'],requests
        assert any('control-' in text for text in control),control
        decoded=[]
        for text in control:
            for token in re.findall(r'[A-Za-z0-9+/=]{40,}',text):
                try:decoded.append(base64.b64decode(token).decode())
                except Exception:pass
        assert any('ip_rollback' in text for text in decoded),decoded
    finally:
        listener.close()
        for transport in transports:transport.close()
