"""Real local SSH/SFTP authentication rotation; no VPS access or system password changes."""
import base64,json,os,shlex,socket,subprocess,tempfile,threading,time
from pathlib import Path
import paramiko

root=Path(__file__).resolve().parents[1]
key=paramiko.RSAKey.generate(2048)
state={'password':'fixture-old'}
class Files(paramiko.SFTPServerInterface):
 def stat(self,path):return paramiko.SFTPAttributes.from_stat(os.stat(path))
 def lstat(self,path):return paramiko.SFTPAttributes.from_stat(os.lstat(path))
 def open(self,path,flags,attr):
  if path.endswith('/agent.py') and state.pop('fail_upload',False):return paramiko.SFTP_PERMISSION_DENIED
  try:
   f=os.fdopen(os.open(path,flags,0o600),'r+b' if flags&(os.O_WRONLY|os.O_RDWR) else 'rb')
   h=paramiko.SFTPHandle(flags);h.readfile=f;h.writefile=f;return h
  except OSError as e:return paramiko.SFTPServer.convert_errno(e.errno)
 def chattr(self,path,attr):paramiko.SFTPServer.set_file_attr(path,attr);return paramiko.SFTP_OK
 def remove(self,path):
  try:os.unlink(path);return paramiko.SFTP_OK
  except OSError as e:return paramiko.SFTPServer.convert_errno(e.errno)
class Server(paramiko.ServerInterface):
 def __init__(self):self.listeners={}
 def check_auth_password(self,user,password):return paramiko.AUTH_SUCCESSFUL if user=='root' and password==state['password'] else paramiko.AUTH_FAILED
 def get_allowed_auths(self,user):return 'password'
 def check_channel_request(self,kind,channel):return paramiko.OPEN_SUCCEEDED
 def check_port_forward_request(self,address,port):
  listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  try:listener.bind(('127.0.0.1',port))
  except OSError:listener.close();return False
  actual=listener.getsockname()[1];self.listeners[actual]=listener;return actual
 def cancel_port_forward_request(self,address,port):
  if port in self.listeners:self.listeners.pop(port).close()
 def check_channel_exec_request(self,channel,command):
  def reply():
   cmd=command.decode();parts=shlex.split(cmd);output='';status=0
   if cmd=='fixture.rotate':state['password']='fixture-new'
   elif cmd=='fixture.fail-sftp':state['fail_upload']=True
   elif cmd=='cat /etc/os-release':output='ID=ubuntu\n'
   elif cmd.startswith('mktemp '):output=tempfile.mkdtemp(prefix='safe-code.',dir='/tmp')+'\n';remote_dirs.append(Path(output.strip()))
   elif '/proc/self/mountinfo' in cmd:output='mounted\n'
   elif 'agent.py' in cmd and len(parts)>1:
    request=json.loads(Path(parts[-1]).read_text());options=request.get('options') or {}
    result={'clientId':options.get('clientId',''),'paths':[m['remote'] for m in options.get('mappings',[])]} if request['op'].startswith('mount_') else {'osId':'ubuntu','statuses':{}}
    output='SC_RESULT:'+json.dumps(result)+'\n'
   if output:channel.sendall(output.encode())
   channel.send_exit_status(status);channel.shutdown_write();time.sleep(.03);channel.close()
  threading.Thread(target=reply,daemon=True).start();return True
with tempfile.TemporaryDirectory(prefix='safecode-credentials-') as directory:
 stop=threading.Event();transports=[];remote_dirs=[];listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
 def connection(sock):
  t=paramiko.Transport(sock);transports.append(t);t.add_server_key(key);t.set_subsystem_handler('sftp',paramiko.SFTPServer,Files);server=Server()
  try:
   t.start_server(server=server)
   while t.is_active() and not stop.is_set():
    channel=t.accept(.1)
    if channel:channels.append(channel)
  except (EOFError,OSError,paramiko.SSHException):pass
  finally:
   for port in list(server.listeners):server.cancel_port_forward_request('127.0.0.1',port)
 channels=[]
 def serve():
  while not stop.is_set():
   try:sock,_=listener.accept()
   except OSError:return
   threading.Thread(target=connection,args=(sock,),daemon=True).start()
 threading.Thread(target=serve,daemon=True).start()
 config=Path(directory)/'fixture.json';config.write_text(json.dumps({'directory':directory,'port':listener.getsockname()[1],'key':base64.b64encode(key.asbytes()).decode()}))
 try:
  subprocess.run([str(root/'.local/dotnet/dotnet'),'run','--project',str(root/'tools/SafeCode.CredentialRecovery.Verify'),'--',str(config)],check=True,timeout=90)
 finally:
  stop.set();listener.close()
  for t in transports:t.close()
  import shutil
  for path in remote_dirs:shutil.rmtree(path)
