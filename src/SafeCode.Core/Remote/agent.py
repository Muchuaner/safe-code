#!/usr/bin/env python3
"""Safe Code Ubuntu 部署端。仅由客户端显式请求运行；不包含服务凭据。"""
import base64,contextlib,fcntl,hashlib,ipaddress,json,os,pwd,re,secrets,shlex,shutil,subprocess,sys,time,tempfile
from pathlib import Path
STATE=Path('/var/lib/safe-code')
REDACTIONS=[]
JOB_DIR=None
class OperationError(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code

def error_code(error):
    if isinstance(error,OperationError):return error.code
    if isinstance(error,json.JSONDecodeError):return 'json_invalid'
    if isinstance(error,PermissionError):return 'access_denied'
    if isinstance(error,subprocess.TimeoutExpired):return 'timeout'
    return 'operation_failed'

LONG_OPERATIONS={'account','localization','desktop','chrome','input','claude','ssh','cloud','mount_install'}
TASKS=['account','localization','desktop','chrome','input','claude','ssh']
def os_info():
    data={}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1);data[k]=v.strip('"')
    return data

def require_ubuntu():
    info=os_info()
    if info.get('ID')!='ubuntu':
        raise RuntimeError('无法安装环境：仅支持 Ubuntu，当前系统为 '+info.get('PRETTY_NAME','未知系统'))
    return info

def result(value):
    if JOB_DIR is not None:write(JOB_DIR/'result.json',json.dumps(value,ensure_ascii=False),0o600)
    print('SC_RESULT:'+json.dumps(value,ensure_ascii=False),flush=True)
def log(message):
    message=str(message)
    for secret in REDACTIONS:
        if secret:message=message.replace(secret,'[已隐藏]')
    line=time.strftime('%H:%M:%S')+' '+message
    print(line,flush=True)
    if STATE.exists():
        with (STATE/'events.log').open('a') as f:f.write(line+'\n')

def run(args,check=True,env=None,stdin=None,timeout=180):
    p=subprocess.run(args,text=True,capture_output=True,input=stdin,env=env,timeout=timeout)
    if check and p.returncode:
        detail=p.stderr or p.stdout or ''
        if 'curl'==Path(args[0]).name:
            host=next((x.split('/')[2] for x in args if isinstance(x,str) and x.startswith('https://')),'下载服务')
            if p.returncode in (5,6) or 'resolving timed out' in detail.lower():
                raise OperationError('dns_failure','无法连接 '+host+'：DNS 域名解析失败，请检查服务器 DNS 或网络后重试。')
            raise OperationError('package_fetch_failed','无法从 '+host+' 下载文件（状态码 '+str(p.returncode)+'），请检查网络后重试。')
        raise RuntimeError(detail[-1600:] or '命令执行失败')
    return p

def stream(args,env=None):
    p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,bufsize=1)
    reason=None
    for line in p.stdout:
        if line.strip():log(line.rstrip())
        lower=line.lower()
        if 'could not resolve' in lower or 'temporary failure resolving' in lower:reason='dns_failure'
        elif 'no space left on device' in lower:reason='disk_full'
        elif 'dpkg was interrupted' in lower:reason='package_interrupted'
        elif 'failed to fetch' in lower and reason not in ['dns_failure','disk_full']:reason='package_fetch_failed'
        elif ('lock-frontend' in lower or 'waiting for cache lock' in lower or 'could not get lock' in lower) and reason is None:reason='package_lock_wait'
    if p.wait():
        hints={'package_lock_wait':'系统包管理器正被占用，请稍后重试。','dns_failure':'服务器无法解析下载域名，请检查 DNS 后重试。','package_fetch_failed':'软件包下载失败，请检查服务器网络后重试。','disk_full':'服务器磁盘空间不足，请清理后重试。','package_interrupted':'上次软件包安装被中断，请修复 dpkg 后重试。'}
        hint=hints.get(reason,'可能是系统正在更新，请稍后重试；如果仍有问题，请提交反馈。' if p.returncode==100 else '请提交反馈。')
        raise OperationError(reason or 'operation_failed','安装命令退出，状态码 '+str(p.returncode)+'。'+hint)

def write(path,content,mode=0o644):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.safe-code-new')
    with temp.open('w') as f:f.write(content)
    temp.chmod(mode);os.replace(temp,path)

def read_config():
    path=STATE/'account.json'
    return json.loads(path.read_text()) if path.exists() else None

def account():
    value=read_config()
    if not value:raise RuntimeError('请先部署工作用户。')
    return value

def home():return Path(pwd.getpwnam(account()['username']).pw_dir)
USER_FILES = r"""
import json,os,pathlib,sys,tempfile
request=json.load(sys.stdin);base=pathlib.Path(os.environ['HOME']).resolve();relative=pathlib.Path(request['relative'])
if relative.is_absolute() or '..' in relative.parts:raise ValueError('用户目录路径无效。')
target=base/relative;operation=request['operation']
checked=target.parent.resolve() if operation in ['alias','check_alias','remove_alias'] else target.resolve()
if not checked.is_relative_to(base):raise ValueError('用户配置路径指向主目录之外，已拒绝操作。')
os.umask(0o077)
if operation=='mkdir':
    if not target.parent.is_dir():raise ValueError('上级文件夹不存在，请重新选择。')
    try:target.mkdir(mode=0o700)
    except FileExistsError:raise ValueError('同名文件或文件夹已存在，请换一个名称。')
    print(json.dumps({'path':str(target),'name':target.name}));sys.exit(0)
if operation=='cloud_onboarding':
    import sqlite3
    if not target.is_file():raise ValueError('CloudCLI 网页数据库尚未初始化，请稍后重试。')
    connection=sqlite3.connect(str(target),timeout=15)
    try:
        required={'id','git_name','git_email','has_completed_onboarding','is_active'}
        if not required.issubset({row[1] for row in connection.execute('PRAGMA table_info(users)')}):raise ValueError('CloudCLI 初始化数据结构不兼容，未修改账号。')
        connection.execute('BEGIN IMMEDIATE')
        previous=[dict(zip(['id','git_name','git_email','has_completed_onboarding'],row)) for row in connection.execute('SELECT id,git_name,git_email,has_completed_onboarding FROM users WHERE is_active=1')]
        backup=target.with_name('onboarding-before-safe-code.json')
        if not backup.exists():
            with backup.open('x') as f:json.dump(previous,f,ensure_ascii=False)
            backup.chmod(0o600)
        name=request['gitName'];email=request['gitEmail']
        connection.execute("UPDATE users SET git_name=CASE WHEN trim(COALESCE(git_name,''))='' THEN ? ELSE git_name END,git_email=CASE WHEN trim(COALESCE(git_email,''))='' THEN ? ELSE git_email END,has_completed_onboarding=1 WHERE is_active=1",(name,email))
        quoted_name=connection.execute('SELECT quote(?)',(name,)).fetchone()[0]
        quoted_email=connection.execute('SELECT quote(?)',(email,)).fetchone()[0]
        connection.execute('DROP TRIGGER IF EXISTS safe_code_cloudcli_onboarding')
        connection.execute("CREATE TRIGGER safe_code_cloudcli_onboarding AFTER INSERT ON users WHEN NEW.is_active=1 BEGIN UPDATE users SET git_name=CASE WHEN trim(COALESCE(git_name,''))='' THEN "+quoted_name+" ELSE git_name END,git_email=CASE WHEN trim(COALESCE(git_email,''))='' THEN "+quoted_email+" ELSE git_email END,has_completed_onboarding=1 WHERE id=NEW.id; END")
        connection.commit();print(json.dumps({'configuredUsers':len(previous)}))
    finally:connection.close()
    sys.exit(0)
if operation=='cloud_projects':
    import sqlite3,uuid
    if not target.is_file():
        print(json.dumps({'ready':False,'changed':False}));sys.exit(0)
    projects=request['projects']
    connection=sqlite3.connect('file:'+str(target)+'?mode=rw',uri=True,timeout=15)
    try:
        required={'project_id','project_path','custom_project_name','isStarred','isArchived'}
        if not required.issubset({row[1] for row in connection.execute('PRAGMA table_info(projects)')}):raise ValueError('CloudCLI 项目数据结构不兼容，未修改项目。')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS safe_code_mount_projects (project_id TEXT PRIMARY KEY, project_path TEXT NOT NULL UNIQUE)')
        changed=False;active=set()
        for project in projects:
            path=project['path'];active.add(path)
            row=connection.execute('SELECT project_id,isArchived FROM projects WHERE project_path=?',(path,)).fetchone()
            if row is None:
                identifier=str(uuid.uuid4())
                connection.execute('INSERT INTO projects(project_id,project_path,custom_project_name,isArchived) VALUES(?,?,?,0)',(identifier,path,project['name']))
                connection.execute('INSERT OR REPLACE INTO safe_code_mount_projects VALUES(?,?)',(identifier,path));changed=True
            elif row[1] and connection.execute('SELECT 1 FROM safe_code_mount_projects WHERE project_id=? AND project_path=?',(row[0],path)).fetchone():
                connection.execute('UPDATE projects SET isArchived=0 WHERE project_id=?',(row[0],));changed=True
        for identifier,path in connection.execute('SELECT project_id,project_path FROM safe_code_mount_projects').fetchall():
            if path not in active:
                cursor=connection.execute('UPDATE projects SET isArchived=1 WHERE project_id=? AND project_path=? AND isArchived=0',(identifier,path));changed=changed or cursor.rowcount>0
        connection.commit();print(json.dumps({'ready':True,'changed':changed,'count':len(projects)}))
    finally:connection.close()
    sys.exit(0)
if operation=='host_bridge':
    import re,subprocess
    client=request['clientId'];action=request['action']
    if not re.fullmatch('[a-f0-9]{20}',client) or relative!=pathlib.Path('.ssh/safe-code-host/'+client+'/identity'):raise ValueError('本机控制标识无效。')
    directory=target.parent;directory.mkdir(mode=0o700,parents=True,exist_ok=True);directory.chmod(0o700)
    config=directory/'config';known=directory/'known_hosts';public=directory/'identity.pub'
    marker='# Safe Code host bridge '+client+'\n'
    if action=='prepare':
        # A fresh dedicated identity for each explicit activation. Never export it.
        for path in [target,public]:path.unlink(missing_ok=True)
        subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-C','safe-code-host-'+client,'-f',str(target)],check=True,capture_output=True,timeout=15)
        target.chmod(0o600);print(json.dumps({'publicKey':public.read_text().strip()}));sys.exit(0)
    if action=='remove':
        for path in [config,known,target,public]:path.unlink(missing_ok=True)
        print(json.dumps({'removed':True}));sys.exit(0)
    if action!='configure':raise ValueError('本机控制操作无效。')
    alias=request['alias'];username=request['username'];port=request['port'];host_key=request['hostKey'];user_sid=request.get('userSid','')
    if not re.fullmatch('[a-z][a-z0-9_-]{0,31}',alias) or not re.fullmatch(r'[a-zA-Z0-9_.@\\-]{1,128}',username):raise ValueError('本机控制别名或用户名无效。')
    is_mac=bool(re.fullmatch(r'macos:[1-9][0-9]{0,9}',user_sid))
    if not is_mac and not re.fullmatch(r'S-1-[0-9]+(?:-[0-9]+){1,15}',user_sid):raise ValueError('本机账户标识无效。')
    if not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('本机控制端口无效。')
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?',host_key):raise ValueError('本机 SSH 指纹材料无效。')
    if not target.is_file():raise ValueError('缺少专用连接密钥，请重新部署本机控制。')
    if config.exists() and not config.read_text().startswith(marker):raise ValueError('本机控制配置被修改，未覆盖。')
    host_alias='safe-code-host-'+client
    body=marker+'Host '+alias+'\n  HostName 127.0.0.1\n  Port '+str(port)+'\n  User '+username+'\n  IdentityFile '+str(target)+'\n  IdentitiesOnly yes\n  BatchMode yes\n  PasswordAuthentication no\n  KbdInteractiveAuthentication no\n  StrictHostKeyChecking yes\n  HostKeyAlias '+host_alias+'\n  UserKnownHostsFile '+str(known)+'\n  GlobalKnownHostsFile /dev/null\n  ForwardAgent no\n  ClearAllForwardings yes\n  ProxyCommand none\n  ProxyJump none\n  PermitLocalCommand no\n  ConnectTimeout 10\n  ServerAliveInterval 15\n  ServerAliveCountMax 2\n'
    def atomic(path,value):
        with tempfile.NamedTemporaryFile('w',dir=directory,delete=False) as f:f.write(value);temporary=pathlib.Path(f.name)
        temporary.chmod(0o600);os.replace(temporary,path)
    atomic(known,host_alias+' '+' '.join(host_key.split()[:2])+'\n');atomic(config,body)
    completed=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'whoami'],capture_output=True,text=True,timeout=20)
    if completed.returncode:raise ValueError('本机 SSH 登录验证失败，请检查本机账户、OpenSSH 日志与授权。')
    identity=completed.stdout.strip().lower().replace('/','\\');expected=username.lower()
    if identity!=expected and not identity.endswith('\\'+expected):raise ValueError('本机登录账户不匹配，已拒绝生成可用状态。')
    if is_mac:
        proof=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'/usr/bin/uname -s && /usr/bin/id -u'],capture_output=True,text=True,timeout=20)
        if proof.returncode or proof.stdout.strip().splitlines()!=['Darwin',user_sid.split(':',1)[1]]:raise ValueError('本机 macOS 账户身份与授权不匹配，未生成可用状态。')
    else:
        import base64
        probe="$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
        encoded=base64.b64encode(probe.encode('utf-16le')).decode()
        proof=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,text=True,errors='replace',timeout=20)
        if proof.returncode or proof.stdout.strip()!=user_sid:raise ValueError('本机 Windows 账户身份与授权不匹配，未生成可用状态。')
    import shlex
    print(json.dumps({'command':'ssh -F '+shlex.quote(str(config))+' '+alias,'verified':True}));sys.exit(0)
if operation=='codex_bridge':
    import base64,fcntl,hashlib,re,shlex,socket,struct,subprocess,time
    if relative!=pathlib.Path('.local/state/safe-code/codex-bridge'):raise ValueError('适配器管理路径无效。')
    key=request.get('publicKey','').strip();parts=key.split();key_id=request.get('keyId','');client=request.get('clientId','')
    if len(parts)<2 or parts[0] not in ['ssh-rsa','ssh-ed25519'] or not re.fullmatch('[A-Za-z0-9+/=]{40,2048}',parts[1]):raise ValueError('适配器公钥无效。')
    if key_id!=hashlib.sha256(parts[1].encode()).hexdigest()[:20] or not re.fullmatch(r'c_[a-f0-9]{24}',client):raise ValueError('适配器客户端标识无效。')
    action=request['action'];state=target;marker=state/'safe-code-component.json';ssh=base/'.ssh';auth=ssh/'authorized_keys'
    if action not in ['install','remove']:raise ValueError('适配器操作无效。')
    for path in [state,ssh,auth]:
        if not path.resolve().is_relative_to(base):raise ValueError('适配器配置路径越界，已拒绝。')
    if auth.is_symlink():raise ValueError('SSH 授权文件为链接，未修改。')
    if state.exists() and any(state.iterdir()) and not marker.is_file():raise ValueError('适配器状态目录已有未知内容，未覆盖。')
    if action=='remove' and not state.exists():print(json.dumps({'ready':False,'warning':None}));sys.exit(0)
    if marker.exists() and (marker.is_symlink() or json.loads(marker.read_text()).get('managedBy')!='safe-code'):raise ValueError('适配器目录标记不属于 Safe Code，未覆盖。')
    state.mkdir(mode=0o700,parents=True,exist_ok=True);state.chmod(0o700)
    def atomic(path,content,mode=0o600):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if not path.parent.resolve().is_relative_to(base) or path.is_symlink():raise ValueError('适配器文件路径异常，未写入。')
        with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as f:f.write(content);tmp=pathlib.Path(f.name)
        tmp.chmod(mode);os.replace(tmp,path)
    if action=='install' and not marker.exists():atomic(marker,json.dumps({'managedBy':'safe-code','sourceHash':request['sourceHash']}))
    owner=state/'clients'/(key_id+'.json');tag='safe-code-codex-'+key_id;warning=None
    with (state/'install.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=auth.read_text() if auth.exists() else ''
        saved=json.loads(owner.read_text()) if owner.exists() else None
        if saved and (saved.get('body')!=parts[1] or saved.get('clientId')!=client):raise ValueError('适配器授权记录不匹配。')
        if action=='remove':
            if saved:
                lines=[line for line in previous.splitlines() if not any(field==parts[1] for field in line.split())]
                if len(lines)!=len(previous.splitlines()):atomic(auth,'\n'.join(lines)+'\n')
                sock_path=state/'run/daemon.sock'
                if sock_path.exists():
                    conn=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);conn.settimeout(12)
                    def send(value):
                        data=json.dumps(value).encode();conn.sendall(struct.pack('>II',len(data),0)+data)
                    def read_n(count):
                        data=b''
                        while len(data)<count:
                            chunk=conn.recv(count-len(data))
                            if not chunk:raise OSError('组件连接已结束。')
                            data+=chunk
                        return data
                    def frame():
                        head,body=struct.unpack('>II',read_n(8))
                        if head>16*1024*1024 or body>4*1024*1024:raise ValueError('适配器状态帧过大。')
                        value=json.loads(read_n(head));read_n(body);return value
                    try:
                        conn.connect(str(sock_path));send({'op':'hello','role':'client','v':1,'clientId':client,'resume':[]})
                        hello=frame()
                        if hello.get('op')!='hello.ok':raise ValueError('适配器未确认客户端状态。')
                        pending={t['turnId'] for t in hello.get('turns',[]) if not t.get('exited')}
                        for turn in pending:send({'op':'turn.cancel','turnId':turn,'reason':'safe-code-stop'})
                        deadline=time.monotonic()+12
                        while pending and time.monotonic()<deadline:
                            event=frame()
                            if event.get('op')=='turn.exit':pending.discard(event.get('turnId'))
                        if pending:warning='本机入口与专用授权已关闭，部分远端任务尚未确认结束。'
                    except Exception:warning='本机入口与专用授权已关闭；远端任务停止未确认，需重连后重试清理。'
                    finally:conn.close()
                if not warning:owner.unlink(missing_ok=True)
            print(json.dumps({'ready':False,'warning':warning}));sys.exit(0)
        if action!='install':raise ValueError('适配器操作无效。')
        digest=request['sourceHash'];node=request['node'];claude=request['claude']
        if not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('适配器版本信息无效。')
        for executable in [node,claude]:
            if not pathlib.Path(executable).is_absolute() or not os.access(executable,os.X_OK):raise ValueError('适配器运行环境不完整，请先部署 Claude Code。')
        help_result=subprocess.run([claude,'--help'],capture_output=True,text=True,errors='replace',timeout=20)
        if help_result.returncode or not all(flag in help_result.stdout+help_result.stderr for flag in ['--tools','--strict-mcp-config']):raise ValueError('Claude Code 不支持所需工具限制，请先更新 Claude Code。')
        if marker.exists() and json.loads(marker.read_text()).get('sourceHash')!=digest and (state/'run/daemon.sock').exists():raise ValueError('旧适配器组件仍在使用，请先结束相关会话再更新。')
        library=base/'.local/share/safe-code/codex-bridge'/digest[:16]
        if not library.resolve().is_relative_to(base):raise ValueError('适配器程序目录越界。')
        for name,encoded in request['files'].items():
            rel=pathlib.Path(name)
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('适配器文件名无效。')
            destination=library/rel;data=base64.b64decode(encoded)
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes()!=data:raise ValueError('适配器程序已有不同内容，未覆盖。')
            else:atomic(destination,data.decode('utf-8-sig'))
        wrapper=state/'bin/connect';command=shlex.quote(str(wrapper))
        script='#!/bin/sh\nif [ "${SSH_ORIGINAL_COMMAND:-}" = "ccw-ping" ]; then echo codex-claude-win-ok; exit 0; fi\nunset SSH_CLIENT SSH_CONNECTION SSH_TTY SSH_AUTH_SOCK SSH_ORIGINAL_COMMAND SSH_USER_AUTH\nexport PATH='+shlex.quote(str(pathlib.Path(node).parent)+':'+str(base/'.local/bin')+':/usr/local/bin:/usr/bin:/bin')+'\nexport CODEX_CLAUDE_WIN_HOME='+shlex.quote(str(state))+'\nexec '+shlex.quote(node)+' '+shlex.quote(str(library/'src/vps/connect.mjs'))+'\n'
        atomic(wrapper,script,0o700)
        policy={'claudePath':claude,'allowedNativeTools':['none','web'],'maxBlobCacheBytes':268435456,'detachGraceMs':30000,'maxConcurrentTurns':4}
        policy_file=state/'policy.json'
        if not policy_file.exists():atomic(policy_file,json.dumps(policy))
        else:
            existing=json.loads(policy_file.read_text())
            if existing.get('claudePath')!=claude or set(existing.get('allowedNativeTools',[]))-{'none','web'}:raise ValueError('适配器策略已被修改，未自动覆盖。')
        ssh.mkdir(mode=0o700,exist_ok=True);ssh.chmod(0o700)
        line='restrict,command="'+command.replace('\\','\\\\').replace('"','\\"')+'" '+parts[0]+' '+parts[1]+' '+tag
        matches=[item for item in previous.splitlines() if parts[1] in item.split()]
        if matches and any(item!=line for item in matches):raise ValueError('此公钥已有不同授权，未放宽或覆盖。')
        if not matches:
            if previous and not (state/'authorized-keys-before-first-install').exists():atomic(state/'authorized-keys-before-first-install',previous)
            atomic(auth,previous.rstrip()+'\n'+line+'\n')
        atomic(owner,json.dumps({'body':parts[1],'clientId':client}));atomic(marker,json.dumps({'managedBy':'safe-code','sourceHash':digest}))
        print(json.dumps({'ready':True,'username':request['username'],'connectCommand':command,'warning':None}));sys.exit(0)
if operation=='install_claude':
    import shutil
    directory=base/'.local/share/claude/versions'
    if not directory.resolve().is_relative_to(base):raise ValueError('Claude Code 安装路径指向主目录之外。')
    directory.mkdir(parents=True,exist_ok=True)
    destination=directory/request['release']
    with tempfile.NamedTemporaryFile(dir=directory,delete=False) as f:
        temporary=pathlib.Path(f.name)
        with open(request['source'],'rb') as source:shutil.copyfileobj(source,f)
    try:
        temporary.chmod(0o755);os.replace(temporary,destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        link=target.with_name('.claude-link-'+str(os.getpid()))
        try:link.symlink_to(destination);os.replace(link,target)
        finally:link.unlink(missing_ok=True)
    finally:temporary.unlink(missing_ok=True)
    sys.exit(0)
if operation=='read':
    if not target.is_file():print(json.dumps(''));sys.exit(0)
    with target.open() as f:value=f.read(1048577)
    if len(value)>1048576:raise ValueError('用户配置文件过大。')
    print(json.dumps(value));sys.exit(0)
if operation in ['alias','check_alias']:
    destination=pathlib.Path(request['destination'])
    if target.is_symlink():
        if target.readlink()!=destination:raise ValueError('目标已存在其他符号链接，未覆盖。')
        sys.exit(0)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):raise ValueError('服务器目录不是空目录，请选择新的或空的目录：'+str(target))
        if operation=='check_alias':sys.exit(0)
        target.rmdir()
    if operation=='check_alias':sys.exit(0)
    target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(destination,target_is_directory=True);sys.exit(0)
if operation=='remove_alias':
    destination=pathlib.Path(request['destination'])
    if target.is_symlink() and target.readlink()==destination:target.unlink();target.mkdir(mode=0o700)
    sys.exit(0)
target.parent.mkdir(parents=True,exist_ok=True)
content=request['content']
if operation=='append_unique':
    if target.exists() and (not target.is_file() or target.stat().st_size>1048576):raise ValueError('配置文件不是普通文件或超过大小限制。')
    previous=target.read_text() if target.exists() else ''
    if content.strip() in previous.splitlines():sys.exit(0)
    content=previous.rstrip()+'\n'+content.rstrip()+'\n'
with tempfile.NamedTemporaryFile('w',dir=target.parent,prefix='.safe-code-',delete=False) as f:
    f.write(content);temporary=pathlib.Path(f.name)
temporary.chmod(request.get('mode',0o644));os.replace(temporary,target)
if str(relative).startswith('.ssh/'):(base/'.ssh').chmod(0o700)
"""
def user_files(operation,relative,**parameters):
    a=account();entry=pwd.getpwnam(a['username'])
    if entry.pw_uid==0:raise RuntimeError('工作账户不能是 root。')
    request={'operation':operation,'relative':str(relative),**parameters}
    command=['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','LANG=C.UTF-8','python3','-I','-c',USER_FILES]
    try:
        if operation=='mkdir':
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            try:output,error=process.communicate(json.dumps(request),timeout=10)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,9)
                except ProcessLookupError:pass
                process.communicate();raise
            completed=subprocess.CompletedProcess(command,process.returncode,output,error)
        else:completed=run(command,check=False,stdin=json.dumps(request))
    except subprocess.TimeoutExpired:raise OperationError('timeout','工作用户目录操作超时，请检查服务器连接或挂载状态后重试。')
    if completed.returncode:
        detail=(completed.stderr or completed.stdout or '用户目录操作失败').strip().splitlines()[-1]
        if detail.startswith('ValueError: '):detail=detail[len('ValueError: '):]
        raise RuntimeError(detail)
    return completed

def user_write(relative,content,mode=0o644):user_files('write',relative,content=content,mode=mode)
def user_read(relative):
    try:return json.loads(user_files('read',relative).stdout)
    except Exception:return ''

def secure_root_directory(path,mode):
    path=Path(path)
    for parent in path.parents:
        data=parent.lstat()
        if parent.is_symlink() or data.st_uid!=0 or data.st_mode&0o022:raise RuntimeError('程序管理目录的上级路径权限不安全：'+str(parent))
    if path.exists() or path.is_symlink():
        data=path.lstat()
        if path.is_symlink() or not path.is_dir() or data.st_uid!=0 or data.st_mode&0o022:raise RuntimeError('程序管理目录权限不安全，未继续安装：'+str(path))
    else:path.mkdir(parents=True,mode=mode)
    path.chmod(mode)

def package_installed(name):
    r=run(['dpkg-query','-W','-f=${db:Status-Status}',name],False)
    return r.returncode==0 and r.stdout=='installed'

def package_environment():
    return {**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l','LC_ALL':'C.UTF-8'}

def package_options():
    return ['apt-get','-o','DPkg::Lock::Timeout=900','-o','Acquire::Retries=3','-o','APT::Update::Error-Mode=any','-o','Dpkg::Options::=--force-confdef','-o','Dpkg::Options::=--force-confold']

def wait_package_manager(timeout=900):
    deadline=time.monotonic()+timeout
    paths=['/var/lib/dpkg/lock-frontend','/var/lib/dpkg/lock','/var/lib/apt/lists/lock','/var/cache/apt/archives/lock']
    while True:
        busy=False
        for path in paths:
            try:
                with open(path,'a') as lock:
                    fcntl.lockf(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    fcntl.lockf(lock,fcntl.LOCK_UN)
            except BlockingIOError:busy=True;break
        if not busy:return
        if time.monotonic()>=deadline:
            raise OperationError('package_lock_wait','系统更新仍在运行，已等待 15 分钟。请等待其完成后重试；不会强行停止更新或删除锁文件。')
        log('系统正在更新，正在等待包管理器完成，请勿关闭服务器…')
        time.sleep(15)

def prepare_system():
    marker=STATE/'system-prepared.json'
    if marker.exists():return
    log('安装前准备：先等待系统更新完成，再更新软件源和系统软件包。')
    wait_package_manager()
    env=package_environment();opts=package_options()
    audit=run(['dpkg','--audit'])
    if audit.stdout.strip():
        log('检测到未完成的软件包配置，正在继续完成…')
        stream(['dpkg','--configure','-a'],env)
    log('正在更新软件源，请稍候…');stream(opts+['update'],env)
    log('正在更新系统软件包，首次安装可能需要较长时间…')
    stream(opts+['upgrade','-y','--with-new-pkgs','--no-remove'],env)
    write(marker,json.dumps({'completedAt':int(time.time())}),0o600)
    log('系统准备完成，继续安装所选组件。')
    if Path('/var/run/reboot-required').exists():log('系统更新提示稍后需要重启；本次不会自动重启服务器。')

def apt(packages):
    missing=[p for p in packages if not package_installed(p)]
    if not missing:log('所需组件已安装，跳过重复下载。');return
    wait_package_manager()
    env=package_environment();opts=package_options()
    stream(opts+['update'],env)
    stream(opts+['install','-y','--no-install-recommends']+missing,env)

def active(unit):return run(['systemctl','is-active','--quiet',unit],False).returncode==0
def version(path):
    path=Path(path)
    if not path.exists():return ''
    args=[str(path),'--version']
    if str(path).startswith('/home/') and os.geteuid()==0:
        a=read_config()
        if not a or pwd.getpwnam(a['username']).pw_uid==0:return ''
        # 用户可修改的程序绝不能在管理员巡检时以 root 执行。
        args=['runuser','-u',a['username'],'--','env','-i','HOME='+pwd.getpwnam(a['username']).pw_dir,'PATH=/usr/bin:/bin','LANG=C.UTF-8','setpriv','--no-new-privs']+args
    process=subprocess.Popen(['timeout','--kill-after=2s','8s']+args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
    try:
        line=process.stdout.readline(4096).strip()
        try:code=process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,9);process.wait();return ''
        return line if code==0 else ''
    finally:process.stdout.close()

def inspect():
    info=os_info();a=read_config() if os.geteuid()==0 else None
    account_ok=bool(a and run(['id',a['username']],False).returncode==0)
    h=Path(pwd.getpwnam(a['username']).pw_dir) if account_ok else Path('/home/workspace')
    xrdp=Path('/etc/xrdp/xrdp.ini')
    loopback=xrdp.exists() and 'port=tcp://127.0.0.1:3389' in xrdp.read_text()
    locale_state=STATE/'localization.json'
    localized=False
    if locale_state.exists():
        desired=json.loads(locale_state.read_text())
        timezone=run(['timedatectl','show','-p','Timezone','--value'],False).stdout.strip()
        localized=timezone==desired.get('timezone') and Path('/etc/default/locale').exists() and desired.get('locale','') in Path('/etc/default/locale').read_text() and all(package_installed(p) for p in locale_packages(desired.get('locale','')))
    pub=user_read('.ssh/authorized_keys') if account_ok else ''
    statuses={
      'account':account_ok and bool(a.get('hostname')) and a['hostname']==os.uname().nodename,
      'localization':localized,
      'desktop':package_installed('xfce4-session') and package_installed('xorgxrdp') and active('xrdp') and loopback,
      'chrome':bool(version('/usr/bin/google-chrome-stable')),
      'input':package_installed('fcitx5-pinyin') and (h/'.config/autostart/fcitx5.desktop').exists(),
      'claude':'Claude Code' in version(h/'.local/bin/claude'),
      'ssh':bool('safe-code-' in pub),
    }
    addresses=[]
    for link in json.loads(run(['ip','-j','-4','address','show'],False).stdout or '[]'):
        for addr in link.get('addr_info',[]):
            if addr.get('scope')=='global':addresses.append(addr['local'])
    mem=int(re.search(r'MemTotal:\s+(\d+)',Path('/proc/meminfo').read_text())[1])*1024
    disk=shutil.disk_usage('/')
    policy=json.loads((STATE/'ip-policy.json').read_text()) if (STATE/'ip-policy.json').exists() else None
    healthy=False
    if policy and shutil.which('nft'):
        import network
        healthy=network.healthy(policy)
    language=Path('/etc/default/locale').read_text() if Path('/etc/default/locale').exists() else ''
    match=re.search(r'(?m)^LANG="?([^\s"]+)',language)
    return {'osId':info.get('ID',''),'osName':info.get('PRETTY_NAME',''),'osVersion':info.get('VERSION_ID',''),
      'architecture':os.uname().machine,'memoryBytes':mem,'diskFreeBytes':disk.free,'cpuCount':os.cpu_count(),
      'hostname':os.uname().nodename, 'homeDirectory':str(h), 'isRoot':os.geteuid()==0,'username':a['username'] if account_ok else '', 'statuses':statuses,
      'assignedIps':addresses,'timezone':run(['timedatectl','show','-p','Timezone','--value'],False).stdout.strip(),
      'systemLocale':match[1] if match else '', 'ipPolicyHealthy':healthy,
      'claudeVersion':version(h/'.local/bin/claude'),'desktopReady':statuses['desktop'],
      'cloudReady':active('safe-code-cloudcli'),'mountReady':package_installed('davfs2'),
      'ipPolicy':policy,'activeJobs':active_jobs() if os.geteuid()==0 else []}

def setup_account(options):
    existing=read_config()
    user=options.get('username') or (existing['username'] if existing else 'workspace')
    hostname=options.get('hostname','').strip() or (existing or {}).get('hostname','') or 'safe-'+secrets.token_hex(4)
    password=options.get('password','')
    if not re.fullmatch('[a-z][a-z0-9_-]{1,30}',user):raise RuntimeError('用户名须为 2–31 位小写字母、数字、下划线或短横线。')
    if hostname and not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',hostname):raise RuntimeError('机器名称须为 1–63 位字母、数字或短横线。')
    if password and (len(password)<12 or len(password)>256 or any(c in password for c in '\r\n\0')):raise RuntimeError('工作密码须为 12–256 位，不能包含换行或空字符。')
    if existing:
        if run(['id',existing['username']],False).returncode:raise RuntimeError('工作用户记录与系统不一致，请手动检查。')
        if user!=existing['username']:
            # 不移动或覆盖已有账户，创建独立环境，旧项目完整保留。
            uid=pwd.getpwnam(existing['username']).pw_uid
            if run(['pgrep','-u',str(uid),'-x','xfce4-session'],False).returncode==0:
                raise RuntimeError('请先在原工作用户的远程桌面中注销，再更换用户名；现有账户、项目和会话均未修改。')
            if any(os.path.ismount(p) for p in Path('/mnt/safe-code').glob('*/*/*')):
                raise RuntimeError('更换用户名需要先卸载目录；原账户与项目不会被删除。')
        else:
            # 先完成不会修改密码的操作，避免机器名设置失败后密码已悄悄改变。
            run(['hostnamectl','set-hostname',hostname])
            if password:
                run(['chpasswd'],stdin=user+':'+password+'\n')
            try:write(STATE/'account.json',json.dumps({'username':user,'password':password or existing['password'],'hostname':hostname}),0o600)
            except Exception as error:
                if password:raise RuntimeError('工作用户密码已更新，但账户记录保存失败。请使用刚设置的新工作密码；服务器登录账户密码未修改。修复配置目录权限或磁盘空间后，可重新应用账户设置。') from error
                raise
            log('工作账户设置已应用。');return
    if run(['id',user],False).returncode==0:raise RuntimeError('此用户已存在，拒绝覆盖其密码。请选择新工作用户名。')
    if existing and Path('/etc/systemd/system/safe-code-cloudcli.service').exists():
        run(['systemctl','disable','--now','safe-code-cloudcli'])
        log('旧工作用户的 CloudCLI 已停止；可为新用户重新安装。')
    password=password or secrets.token_urlsafe(20)
    run(['hostnamectl','set-hostname',hostname])
    run(['useradd','-m','-s','/bin/bash',user])
    run(['chpasswd'],stdin=user+':'+password+'\n')
    try:write(STATE/'account.json',json.dumps({'username':user,'password':password,'hostname':hostname}),0o600)
    except Exception as error:raise RuntimeError('工作用户已创建并设置密码，但账户记录保存失败。请保留刚设置的工作用户名和密码，通过服务器控制台检查配置目录权限或磁盘空间；服务器登录账户密码未修改。') from error
    user_write('workspace/.keep','')
    if package_installed('xfce4-session'):setup_xsession()
    log('工作账户、机器名称与项目目录已创建。')

def list_directories(options):
    a=account();entry=pwd.getpwnam(a['username'])
    # 浏览以普通工作用户身份执行，不泄露管理员专属目录。
    script="""import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve(strict=True)
if not p.is_dir():raise ValueError('目录不存在')
items=[]
for child in os.scandir(p):
 try:
  if child.is_dir(follow_symlinks=False) or child.is_symlink():items.append({'name':child.name,'path':str(p/child.name)})
 except OSError:pass
 if len(items)>=300:break
print(json.dumps({'path':str(p),'parent':str(p.parent),'directories':sorted(items,key=lambda x:x['name'].casefold()),'truncated':len(items)>=300}))
"""
    path=options.get('path') or str(Path(entry.pw_dir)/'workspace')
    if not isinstance(path,str) or len(path)>4096 or '\0' in path:raise RuntimeError('目录路径无效。')
    log('以工作用户读取目录：'+path)
    try:
        output=subprocess.run(['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','python3','-I','-c',script,path],capture_output=True,text=True,timeout=8)
    except subprocess.TimeoutExpired:raise RuntimeError('读取目录超时，请检查网络或失效的挂载目录后重试。')
    if output.returncode:
        if 'PermissionError' in output.stderr:raise RuntimeError('工作用户没有读取该目录的权限，请选择其他目录。')
        if 'FileNotFoundError' in output.stderr:raise RuntimeError('目录不存在，请返回上一级或填写其他目录。')
        raise RuntimeError('无法读取该目录，请检查目录状态后重试。')
    log('目录读取完成。')
    return json.loads(output.stdout)

def create_directory(options):
    name=options.get('name','').strip();parent=options.get('path','') or str(home()/'workspace')
    if not name or len(name)>128 or name in ('.','..') or any(c in name for c in '/\\\0\r\n'):raise RuntimeError('文件夹名称须为 1–128 个字符，不能包含路径分隔符或换行。')
    target=Path(parent)/name
    if not Path(parent).is_absolute() or '..' in Path(parent).parts or not target.is_relative_to(home()):raise RuntimeError('请在工作用户主目录内新建文件夹。')
    return json.loads(user_files('mkdir',str(target.relative_to(home()))).stdout)

def locale_packages(locale):
    language=locale.split('_',1)[0]
    if not re.fullmatch('[a-z]{2}',language):raise RuntimeError('系统语言无效。')
    if language=='zh':language='zh-hant' if locale.startswith(('zh_TW','zh_HK','zh_MO')) else 'zh-hans'
    return ['language-pack-'+language,'language-pack-gnome-'+language]

def setup_locale(options):
    account()
    timezone=options.get('timezone','');locale=options.get('locale','')
    if not timezone or '..' in timezone or not (Path('/usr/share/zoneinfo')/timezone).is_file():raise RuntimeError('请选择有效的 IANA 时区。')
    if not re.fullmatch('[a-z]{2}_[A-Z]{2}\\.UTF-8',locale):raise RuntimeError('请选择有效的 UTF-8 系统语言。')
    apt(['locales','tzdata']+locale_packages(locale))
    supported=Path('/usr/share/i18n/SUPPORTED').read_text()
    if locale+' UTF-8' not in supported:raise RuntimeError('Ubuntu 不支持此 Locale。')
    log('设置时区：'+timezone);run(['timedatectl','set-timezone',timezone])
    log('生成语言环境：'+locale);stream(['locale-gen',locale]);run(['update-locale','LANG='+locale])
    write(STATE/'localization.json',json.dumps({'timezone':timezone,'locale':locale}),0o600)
    setup_xsession()

def setup_xsession():
    if not read_config():return
    locale='en_US.UTF-8'
    if (STATE/'localization.json').exists():locale=json.loads((STATE/'localization.json').read_text())['locale']
    user_write('.xsession','#!/bin/sh\nunset DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER\nexec startxfce4\n',0o755)
    user_write('.xsessionrc','export LANG='+locale+'\nexport XMODIFIERS="@im=fcitx"\nexport GTK_IM_MODULE=fcitx\nexport QT_IM_MODULE=fcitx\n')

def setup_desktop(options):
    account();apt(['xfce4-session','xfwm4','xfce4-panel','xfdesktop4','xfce4-settings','xfce4-terminal','thunar','dbus-x11','xrdp','xorgxrdp','xauth','x11-utils','x11-xserver-utils','xinput','xdg-user-dirs','fonts-noto-cjk'])
    config=Path('/etc/xrdp/xrdp.ini');text=config.read_text()
    new,count=re.subn(r'(?m)^port=(?:3389|tcp://[^\n]+:3389)\s*$', 'port=tcp://127.0.0.1:3389',text,count=1)
    if count!=1:raise RuntimeError('无法安全识别 xrdp 监听设置，未修改配置。')
    if not (STATE/'backup-xrdp.ini').exists():write(STATE/'backup-xrdp.ini',text,0o600)
    write(config,new)
    config=Path('/etc/xrdp/sesman.ini');text=config.read_text()
    if not (STATE/'backup-sesman.ini').exists():write(STATE/'backup-sesman.ini',text,0o600)
    for key,value in {'AllowRootLogin':'false','MaxSessions':'1','KillDisconnected':'false','DisconnectedTimeLimit':'0','IdleTimeLimit':'0'}.items():
        text=re.sub(r'(?m)^'+key+r'=.*$',key+'='+value,text)
    write(config,text)
    setup_xsession();run(['usermod','-aG','ssl-cert','xrdp'])
    user_write('.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml','<?xml version="1.0"?><channel name="xfwm4" version="1.0"><property name="general" type="empty"><property name="use_compositing" type="bool" value="false"/></property></channel>')
    run(['systemctl','enable','xrdp']);run(['systemctl','restart','xrdp'])
    if not active('xrdp'):raise RuntimeError('xrdp 启动失败，请检查系统日志。')
    log('XFCE 与 xrdp 已就绪。RDP 仅监听回环地址，通过 SSH 连接。')

def setup_chrome(options):
    account()
    if os.uname().machine!='x86_64':raise RuntimeError('Google Chrome 当前仅支持本安装器的 x86_64 Ubuntu。')
    apt(['curl','ca-certificates','gnupg'])
    key=run(['curl','-fsSL','--retry','3','https://dl.google.com/linux/linux_signing_key.pub']).stdout
    with tempfile.TemporaryDirectory(prefix='safe-code-google-') as temporary:
        output=Path(temporary)/'google-key.gpg'
        run(['gpg','--batch','--yes','--dearmor','--output',str(output)],stdin=key)
        keybytes=output.read_bytes()
    Path('/etc/apt/keyrings').mkdir(exist_ok=True)
    target=Path('/etc/apt/keyrings/safe-code-google.gpg');target.write_bytes(keybytes);target.chmod(0o644)
    write('/etc/apt/sources.list.d/safe-code-google.sources','Types: deb\nURIs: https://dl.google.com/linux/chrome/deb/\nSuites: stable\nComponents: main\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/safe-code-google.gpg\n')
    apt(['google-chrome-stable'])
    # 从当前桌面环境启动，保留 Chrome 沙箱，不使用 --no-sandbox。
    user_write('Desktop/Chrome.desktop','[Desktop Entry]\nType=Application\nName=Chrome\nExec=google-chrome-stable\nIcon=google-chrome\nTerminal=false\n',0o755)
    log(version('/usr/bin/google-chrome-stable'))

def setup_input(options):
    account();apt(['fcitx5','fcitx5-pinyin','fcitx5-frontend-gtk3','fcitx5-frontend-qt5','fonts-noto-cjk'])
    setup_xsession()
    user_write('.config/fcitx5/profile','[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=pinyin\n\n[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n[Groups/0/Items/1]\nName=pinyin\nLayout=\n\n[GroupOrder]\n0=Default\n')
    user_write('.config/autostart/fcitx5.desktop','[Desktop Entry]\nType=Application\nName=Fcitx5\nExec=fcitx5 -d\nTerminal=false\n')
    log('拼音与中文字体已就绪，重新登录桌面后生效。')

def file_sha256(file):
    digest=hashlib.sha256()
    for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def download_claude():
    import platform,socket,urllib.request,urllib.error
    target_platform={'x86_64':'linux-x64','aarch64':'linux-arm64'}.get(platform.machine())
    if not target_platform:raise RuntimeError('Claude Code 当前仅支持 x86_64 或 ARM64 Ubuntu。')
    base='https://downloads.claude.ai/claude-code-releases'
    target=Path(__file__).resolve().parent/('claude-payload-'+secrets.token_hex(16))
    def fetch(url):return urllib.request.urlopen(url,timeout=45)
    try:
        log('正在通过 VPS 网络获取 Claude Code 官方版本和校验信息…')
        with fetch(base+'/stable') as response:release=response.read(128).decode().strip()
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',release):raise RuntimeError('Claude Code 官方版本信息无效。')
        with fetch(base+'/'+release+'/manifest.json') as response:manifest=json.loads(response.read(1024*1024))
        digest=manifest['platforms'][target_platform]['checksum']
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise RuntimeError('Claude Code 官方校验信息无效。')
        received=0;progress=time.monotonic();started=progress
        with fetch(base+'/'+release+'/'+target_platform+'/claude') as response, target.open('xb') as output:
            target.chmod(0o600)
            while True:
                block=response.read(1024*1024)
                if not block:break
                received+=len(block)
                if received>1024**3:raise RuntimeError('Claude Code 下载文件异常过大，已停止安装。')
                if time.monotonic()-started>600:raise TimeoutError('下载超时')
                output.write(block)
                if time.monotonic()-progress>=10:
                    log('VPS 正在下载 Claude Code：已接收 '+str(received//1024//1024)+' MB…');progress=time.monotonic()
        with target.open('rb') as source:
            if file_sha256(source)!=digest:raise RuntimeError('Claude Code 下载文件校验失败，未执行安装。')
        log('VPS 下载完成，官方 Claude Code 文件校验通过。')
        return {'claudePayload':str(target),'claudeSha256':digest,'claudeVersion':release}
    except Exception as error:
        target.unlink(missing_ok=True)
        if isinstance(error,(TimeoutError,socket.timeout)):raise OperationError('timeout','VPS 下载 Claude Code 官方文件超时。') from error
        if isinstance(error,urllib.error.URLError):
            dns=isinstance(error.reason,socket.gaierror)
            raise OperationError('dns_failure' if dns else 'package_fetch_failed','VPS 无法连接 Claude Code 官方下载服务'+('：DNS 域名解析失败。' if dns else '。')) from error
        raise

def setup_claude(options):
    a=account();apt(['curl','ca-certificates','git','tmux'])
    payload=options.get('claudePayload')
    if payload:
        source=Path(payload);expected=options.get('claudeSha256','');release=options.get('claudeVersion','')
        if source.parent!=Path(__file__).resolve().parent or source.is_symlink() or not re.fullmatch('claude-payload-[a-f0-9]{32}',source.name):raise RuntimeError('Claude Code 安装文件路径无效。')
        if not re.fullmatch('[a-f0-9]{64}',expected) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',release):raise RuntimeError('Claude Code 校验信息无效。')
        with source.open('rb') as f:
            if file_sha256(f)!=expected:raise RuntimeError('Claude Code 文件校验失败，未执行安装。')
        staged=Path('/opt/safe-code')/('claude-verified-'+expected)
        with tempfile.NamedTemporaryFile(dir='/opt/safe-code',delete=False) as f:
            temporary=Path(f.name)
            with source.open('rb') as original:shutil.copyfileobj(original,f)
        try:
            with temporary.open('rb') as f:
                if file_sha256(f)!=expected:raise RuntimeError('Claude Code 文件校验失败，未执行安装。')
            temporary.chmod(0o555);os.replace(temporary,staged)
            log('官方 Claude Code 文件校验通过，正在为工作用户安装…')
            user_files('install_claude','.local/bin/claude',source=str(staged),release=release)
        finally:temporary.unlink(missing_ok=True);staged.unlink(missing_ok=True);source.unlink(missing_ok=True)
    else:
        target=Path('/opt/safe-code/install-claude.sh')
        script=run(['curl','-fsSL','--connect-timeout','10','--max-time','60','--retry','2','https://claude.ai/install.sh']).stdout
        write(target,script,0o755)
        log('使用官方原生安装器为普通工作用户安装 Claude Code。')
        stream(['runuser','-u',a['username'],'--','env','-i','HOME='+str(home()),'PATH=/usr/local/bin:/usr/bin:/bin','LANG=C.UTF-8','bash',str(target),'stable'])
    value=version(home()/'.local/bin/claude')
    if not value:raise RuntimeError('Claude Code 安装后未找到有效程序。')
    user_files('append_unique','.profile',content='export PATH="$HOME/.local/bin:$PATH"',mode=0o644)
    log(value+'；请由本人完成 Claude 账号授权。')

def setup_ssh(options):
    account();public=options.get('publicKey','').strip()
    if not re.fullmatch(r'(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+ safe-code-[A-Za-z0-9_-]+',public):raise RuntimeError('客户端 SSH 公钥格式无效。')
    user_files('append_unique','.ssh/authorized_keys',content=public,mode=0o600)
    user_write('.local/bin/safe-code-terminal','#!/bin/sh\ncd "$HOME/workspace"\nexec tmux new-session -A -s safe-code\n',0o755)
    log('工作用户公钥已授权，原 SSH 配置及管理入口保持。')

def install_mount(options):apt(['davfs2','fuse3']);log('目录挂载组件已安装。')

def setup_git_identity():
    a=account();entry=pwd.getpwnam(a['username'])
    if entry.pw_uid==0:raise RuntimeError('Git 身份必须配置在普通工作用户下。')
    if not shutil.which('git'):raise RuntimeError('尚未安装 Git，请先安装 Claude Code 或重新安装 CloudCLI。')
    command=['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','LANG=C.UTF-8','git','config','--global']
    identity={}
    for key,default in [('user.name',a['username']),('user.email',a['username']+'@safe-code.invalid')]:
        existing=run(command+['--get',key],check=False)
        if existing.returncode not in (0,1):raise RuntimeError('无法读取工作用户的 Git 配置，请检查文件权限。')
        value=existing.stdout.strip()
        if not value:
            run(command+[key,default]);value=default
            log('已补全本地 Git 默认身份，可在 CloudCLI 设置中修改。')
        identity[key]=value
    return identity

def setup_cloud_onboarding(options):
    identity=setup_git_identity()
    report=json.loads(user_files('cloud_onboarding','.local/state/safe-code-cloud/auth.db',gitName=identity['user.name'],gitEmail=identity['user.email']).stdout)
    log('CloudCLI 首次设置已完成，保留现有账号与 Git 身份；可直接进入主界面。')
    sync_cloud_mount_projects()
    return report

def setup_cloud(options):
    secure_root_directory('/mnt/safe-code',0o755)
    a=account();apt(['curl','xz-utils','ca-certificates','git'])
    setup_git_identity()
    # Node 独立放置，避免覆盖用户已有运行时。
    node_root=Path('/opt/safe-code/node')
    if not (node_root/'bin/node').exists():
        index=json.loads(run(['curl','-fsSL','https://nodejs.org/dist/index.json']).stdout)
        release=next(x for x in index if x['version'].startswith('v22.') and x.get('lts'))
        v=release['version'];name='node-'+v+'-linux-x64.tar.xz';url='https://nodejs.org/dist/'+v+'/'
        sums=run(['curl','-fsSL',url+'SHASUMS256.txt']).stdout
        expected=next(line.split()[0] for line in sums.splitlines() if line.split()[-1]==name)
        archive=Path('/opt/safe-code')/name
        stream(['curl','-fL','--retry','3',url+name,'-o',str(archive)])
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise RuntimeError('Node 下载校验失败。')
        run(['tar','-xJf',str(archive),'-C','/opt/safe-code']);os.rename('/opt/safe-code/'+name[:-7],node_root)
    app=Path('/opt/safe-code/cloudcli');app.mkdir(exist_ok=True)
    env={**os.environ,'PATH':str(node_root/'bin')+':'+os.environ['PATH'],'SHARP_IGNORE_GLOBAL_LIBVIPS':'1'}
    stream([str(node_root/'bin/npm'),'install','--prefix',str(app),'--omit=optional','--no-audit','--no-fund','@cloudcli-ai/cloudcli@1.37.7'],env)
    # 补全终端搜索所需的平台程序，仍使用官方 npm registry。
    stream([str(node_root/'bin/npm'),'install','--prefix',str(app),'--no-audit','--no-fund','@vscode/ripgrep-linux-x64'],env)
    cli=app/'node_modules/@cloudcli-ai/cloudcli/dist-server/server/index.js'
    if not cli.exists():raise RuntimeError('CloudCLI 缺少服务入口，未创建服务。')
    user_write('.local/state/safe-code-cloud/.keep','')
    unit='''[Unit]\nDescription=Safe Code CloudCLI\nAfter=network-online.target\n[Service]\nType=simple\nUser={user}\nWorkingDirectory={home}\nEnvironment=HOME={home}\nEnvironment=HOST=127.0.0.1\nEnvironment=SERVER_PORT=3001\nEnvironment=DATABASE_PATH={home}/.local/state/safe-code-cloud/auth.db\nEnvironment=NODE_ENV=production\nEnvironment=NODE_OPTIONS=--max-old-space-size=384\nEnvironment=CLAUDE_CLI_PATH={home}/.local/bin/claude\nEnvironment=PATH={node}/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin\nExecStart={node}/bin/node {cli}\nRestart=on-failure\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=read-only\nReadWritePaths={home} /mnt/safe-code\nMemoryMax=1G\n[Install]\nWantedBy=multi-user.target\n'''.format(user=a['username'],home=home(),node=node_root,cli=cli)
    path=Path('/etc/systemd/system/safe-code-cloudcli.service');changed=not path.exists() or path.read_text()!=unit
    write(path,unit)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','safe-code-cloudcli'])
    if changed:run(['systemctl','restart','safe-code-cloudcli'])
    time.sleep(3)
    if not active('safe-code-cloudcli'):raise RuntimeError('CloudCLI 启动失败，请检查专属服务日志。')
    setup_cloud_onboarding(options)
    log('CloudCLI 已就绪。首次创建网页账户后可直接进入主界面，Claude 账号授权由本人完成。')

def setup_cloud_public(options):
    require_ubuntu()
    service_path=Path('/etc/systemd/system/safe-code-cloudcli.service')
    if not service_path.exists() or not active('safe-code-cloudcli'):
        log('正在准备 CloudCLI 基础服务…')
        setup_cloud(options)
    log('正在配置 CloudCLI 允许公网访问 (0.0.0.0:3001)…')
    content=service_path.read_text()
    if 'Environment=HOST=127.0.0.1' in content:
        content=content.replace('Environment=HOST=127.0.0.1','Environment=HOST=0.0.0.0')
        write(service_path,content)
        run(['systemctl','daemon-reload'])
        log('正在重启 CloudCLI 服务以应用配置…')
        run(['systemctl','restart','safe-code-cloudcli'])
        time.sleep(3)
    elif not active('safe-code-cloudcli'):
        run(['systemctl','restart','safe-code-cloudcli'])
        time.sleep(3)
    if not active('safe-code-cloudcli'):
        raise RuntimeError('CloudCLI 启动失败，请检查专属服务日志。')
    log('正在检查防火墙配置…')
    if shutil.which('ufw'):
        try:
            status=run(['ufw','status'],False).stdout
            if 'Status: active' in status:
                run(['ufw','allow','3001/tcp'],False)
                log('已在 UFW 防火墙放行 TCP 3001 端口。')
        except Exception:
            pass
    log('正在获取公网访问地址…')
    ip=(options or {}).get('host') or (options or {}).get('ip')
    if not ip or ip in ['127.0.0.1','localhost']:
        try:
            ip=run(['curl','--noproxy','*','-4','-fsS','--connect-timeout','4','--max-time','8','https://api.ipify.org'],False).stdout.strip()
        except Exception:
            pass
    if not ip:
        for link in json.loads(run(['ip','-j','-4','address','show'],False).stdout or '[]'):
            for addr in link.get('addr_info',[]):
                if addr.get('scope')=='global':
                    ip=addr['local']
                    break
            if ip:break
    url=f'http://{ip}:3001'
    log(f'CloudCLI 已成功开放公网访问：{url}')
    log('提示：请确保网页密码安全。')
    return {'url':url,'ip':ip,'port':3001,'cloudReady':True}

# A small companion to the installed CloudCLI frontend. Reuse its normal refresh
# event instead of reloading the page or fabricating a session. Only /ws is used;
# terminal sockets and streamed model messages are left alone.
CLOUD_MOUNT_REFRESH = r"""(() => {
  if (window.__safeCodeMountRefresh) return;
  window.__safeCodeMountRefresh = true;
  const sockets = new Set(), NativeWebSocket = window.WebSocket;
  window.WebSocket = new Proxy(NativeWebSocket, {
    construct(Target, args) {
      const socket = new Target(...args);
      if (new URL(args[0], location.href).pathname === '/ws') {
        sockets.add(socket);
        socket.addEventListener('close', () => sockets.delete(socket), {once:true});
      }
      return socket;
    }
  });
  let revision, checking = false;
  async function check() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch('/safe-code-mount-revision.json', {cache:'no-store'});
      if (!response.ok) return;
      const value = await response.json();
      if (typeof value.revision !== 'string') return;
      if (revision !== value.revision) {
        let delivered = false;
        for (const socket of sockets) {
          if (socket.readyState !== NativeWebSocket.OPEN) continue;
          socket.dispatchEvent(new MessageEvent('message', {data:JSON.stringify({kind:'websocket_reconnected'})}));
          delivered = true;
        }
        if (!delivered) return;
      }
      revision = value.revision;
    } catch {} finally { checking = false; }
  }
  setInterval(check, 3000);
  document.addEventListener('visibilitychange', check);
  check();
})();
"""

def cloud_project_targets():
    """Read mountinfo and trusted records without traversing a disconnected FUSE."""
    mounted={line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()}
    managed=Path('/mnt/safe-code')/account()['username'];projects={}
    for record in STATE.glob('mount-*.json'):
        if not re.fullmatch(r'mount-[a-f0-9]{20}\.json',record.name):continue
        data=json.loads(record.read_text())
        for alias,path in zip(data.get('aliases',[]),data.get('paths',[])):
            # Records belonging to a different work user are never registered.
            if path not in mounted or '..' in Path(path).parts or not Path(path).is_relative_to(managed):continue
            alias=Path(alias)
            if not alias.is_absolute() or '..' in alias.parts or not alias.is_relative_to(home()) or alias==home():continue
            projects[path]={'path':path,'name':alias.name}
    return list(projects.values())

def sync_cloud_mount_projects():
    """Best effort: project discovery must never tear down a working file bridge."""
    app=Path('/opt/safe-code/cloudcli/node_modules/@cloudcli-ai/cloudcli')
    if not app.exists():return None
    try:
        # Scope the integration to the API/event format shipped by our installer.
        if not (app/'dist-server/server/modules/database/repositories/projects.db.js').is_file():raise RuntimeError('CloudCLI 版本暂不支持项目联动，请更新后重新打开。')
        index=app/'dist/index.html'
        if index.is_symlink() or not index.is_file():raise RuntimeError('CloudCLI 网页入口异常，未修改网页。')
        report=json.loads(user_files('cloud_projects','.local/state/safe-code-cloud/auth.db',projects=cloud_project_targets()).stdout)
        if not report['ready']:return None
        script=app/'dist/safe-code-mounts.js';revision=app/'dist/safe-code-mount-revision.json'
        # These assets contain no paths, account data or authentication tokens.
        write(script,CLOUD_MOUNT_REFRESH)
        html=index.read_text();tag='<script src="/safe-code-mounts.js" data-safe-code-mounts></script>'
        if 'data-safe-code-mounts' not in html:
            if '<head>' not in html:raise RuntimeError('CloudCLI 网页入口不兼容，未修改网页。')
            backup=index.with_name('index.before-safe-code-mounts.html')
            if not backup.exists():write(backup,html)
            write(index,html.replace('<head>','<head>\n    '+tag,1))
        if report['changed'] or not revision.exists():write(revision,json.dumps({'revision':secrets.token_hex(16)}))
        log('CloudCLI 已同步挂载项目；网页项目列表会自动刷新。')
        return None
    except Exception as error:
        warning='目录挂载保持正常，CloudCLI 项目联动未完成：'+str(error)
        log(warning);return warning

CODEX_BRIDGE_SOURCE_HASH='3d6c8d98604e7301bd92046b00ead3b08eddefa157e1c5661d2c9599aefe58c3'
def codex_bridge(options):
    action=options.get('action','');a=account();parameters={k:options[k] for k in ['publicKey','keyId','clientId']}
    if action=='install':
        import zipfile
        payload=Path(options.get('payload',''))
        if payload.parent!=Path(__file__).resolve().parent or payload.is_symlink() or not re.fullmatch(r'codex-bridge-[a-f0-9]{32}\.zip',payload.name):raise RuntimeError('适配器源码包路径无效。')
        data=payload.read_bytes()
        if hashlib.sha256(data).hexdigest()!=CODEX_BRIDGE_SOURCE_HASH:raise RuntimeError('适配器源码包校验失败，未安装。')
        node=Path('/opt/safe-code/node/bin/node')
        if not node.exists():
            candidate=shutil.which('node')
            if candidate and int(run([candidate,'-p','process.versions.node.split(".")[0]']).stdout.strip())>=20:node=Path(candidate)
            else:
                apt(['curl','xz-utils','ca-certificates'])
                index=json.loads(run(['curl','-fsSL','https://nodejs.org/dist/index.json']).stdout);release=next(x for x in index if x['version'].startswith('v22.') and x.get('lts'))
                version=release['version'];arch={'x86_64':'x64','aarch64':'arm64'}.get(os.uname().machine)
                if not arch or not re.fullmatch(r'v22\.[0-9]+\.[0-9]+',version):raise RuntimeError('当前架构或 Node.js 版本不支持适配器自动安装。')
                name='node-'+version+'-linux-'+arch+'.tar.xz';url='https://nodejs.org/dist/'+version+'/'
                expected=next(line.split()[0] for line in run(['curl','-fsSL',url+'SHASUMS256.txt']).stdout.splitlines() if line.split()[-1]==name)
                archive=Path('/opt/safe-code')/name;stream(['curl','-fL','--retry','3',url+name,'-o',str(archive)])
                if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise RuntimeError('Node.js 文件校验失败。')
                with tempfile.TemporaryDirectory(prefix='node-bridge-',dir='/opt/safe-code') as stage:
                    run(['tar','-xJf',str(archive),'-C',stage]);candidate=Path(stage)/name[:-7]
                    run([str(candidate/'bin/node'),'--version']);os.rename(candidate,node.parent.parent)
        if int(run([str(node),'-p','process.versions.node.split(".")[0]']).stdout.strip())<20:raise RuntimeError('适配器需要 Node.js 20 或更高版本。')
        claude=home()/'.local/bin/claude'
        if not claude.exists():raise RuntimeError('请先为工作用户部署 Claude Code，并自行完成 Claude 登录。')
        with zipfile.ZipFile(payload) as archive:
            files={name:base64.b64encode(archive.read(name)).decode() for name in archive.namelist()}
        parameters.update({'files':files,'node':str(node),'claude':str(claude),'sourceHash':CODEX_BRIDGE_SOURCE_HASH,'username':a['username']})
    return json.loads(user_files('codex_bridge','.local/state/safe-code/codex-bridge',action=action,**parameters).stdout)

def host_bridge(options):
    client=options.get('clientId','');action=options.get('action','')
    if not re.fullmatch('[a-f0-9]{20}',client):raise RuntimeError('本机控制标识无效。')
    if action=='configure':
        port=options.get('port')
        if not isinstance(port,int) or not 1024<=port<=65535:raise RuntimeError('本机控制端口无效。')
        listeners=run(['ss','-H','-lnt','sport = :'+str(port)]).stdout
        if '127.0.0.1:'+str(port) not in listeners or any(x in listeners for x in ['0.0.0.0:'+str(port),'*:'+str(port),'[::]:'+str(port)]):raise RuntimeError('本机控制必须只绑定服务器回环地址。')
    return json.loads(user_files('host_bridge','.ssh/safe-code-host/'+client+'/identity',**options).stdout)

def mount_client(options):
    client=options.get('clientId','')
    if not re.fullmatch('[a-f0-9]{20}',client):raise RuntimeError('挂载客户端标识无效。')
    return client

def remove_mounts(options):
    client=mount_client(options);record=STATE/('mount-'+client+'.json')
    if not record.exists():return {'clientId':client,'paths':[]}
    previous=json.loads(record.read_text())
    for target in reversed(previous.get('paths',[])):
        if os.path.ismount(target):
            log('同步并卸载目录：'+target)
            completed=subprocess.run(['umount',target],capture_output=True,text=True,timeout=90)
            if completed.returncode:raise RuntimeError('目录仍在使用，未关闭文件通道：'+target+'；'+completed.stderr[-400:])
    for alias,destination in zip(previous.get('aliases',[]),previous.get('paths',[])):
        try:user_files('remove_alias',str(Path(alias).relative_to(home())),destination=destination)
        except Exception:log('用户已修改目录入口，保留该入口，不执行管理员删除。')
    secrets_file=Path('/etc/davfs2/secrets')
    if secrets_file.exists():
        lines=secrets_file.read_text().splitlines()
        urls=set(previous.get('urls',[]))
        lines=[line for line in lines if not any(line.startswith(url+' ') for url in urls) and line!='# safe-code-'+client]
        write(secrets_file,'\n'.join(lines)+'\n',0o600)
    record.unlink();warning=sync_cloud_mount_projects() if options.get('syncProjects',True) else None
    return {'clientId':client,'paths':[],'cloudWarning':warning}

def mount_project_paths(client,aliases,previous_targets):
    managed=Path('/mnt/safe-code')/account()['username']/client
    registry=STATE/('mount-project-paths-'+client+'.json')
    saved=json.loads(registry.read_text()) if registry.exists() else {}
    targets=[previous_targets.get(alias,saved.get(alias,str(managed/('p'+hashlib.sha256(alias.encode()).hexdigest()[:20])))) for alias in aliases]
    if len(set(targets))!=len(targets) or any(Path(path).parent!=managed or not re.fullmatch(r'm[0-9]+|p[a-f0-9]{20}',Path(path).name) for path in targets):raise RuntimeError('挂载项目路径记录异常，已保留现有挂载。')
    return targets,registry,{**saved,**dict(zip(aliases,targets))}

def apply_mounts(options,recover=False):
    a=account();client=mount_client(options)
    if not package_installed('davfs2'):raise RuntimeError('请先安装目录挂载组件。')
    port=options.get('port');user=options.get('username');password=options.get('password','');mappings=options.get('mappings',[])
    if not isinstance(port,int) or not 1024<=port<=65535 or user!='safecode' or not re.fullmatch('[a-f0-9]{48}',password):raise RuntimeError('文件通道认证参数无效。')
    if not 1<=len(mappings)<=12:raise RuntimeError('请选择 1–12 组目录。')
    REDACTIONS.append(password)
    listeners=run(['ss','-H','-lnt','sport = :'+str(port)]).stdout
    if '127.0.0.1:'+str(port) not in listeners or any(x in listeners for x in ['0.0.0.0:'+str(port),'*:'+str(port),'[::]:'+str(port)]):raise RuntimeError('反向文件端口未限定为回环地址，请检查 sshd 的 GatewayPorts 设置。')
    aliases=[]
    previous_record=STATE/('mount-'+client+'.json')
    previous=json.loads(previous_record.read_text()) if previous_record.exists() else {}
    previous_targets=dict(zip(previous.get('aliases',[]),previous.get('paths',[])))
    log('正在检查服务器目录…')
    for mapping in mappings:
        value=mapping.get('remote','');target=Path(value)
        if not target.is_absolute() or '..' in target.parts or not target.parent.resolve().is_relative_to(home()):raise RuntimeError('挂载目标必须位于工作用户的主目录下。')
        if target==home():raise RuntimeError('不能覆盖整个用户目录。')
        if str(target) in aliases:raise RuntimeError('挂载目标不能重复。')
        if any(target.is_relative_to(Path(alias)) or Path(alias).is_relative_to(target) for alias in aliases):raise RuntimeError('挂载目录不能相互包含，请选择独立的目录。')
        user_files('check_alias',str(target.relative_to(home())),destination=previous_targets.get(str(target),''))
        aliases.append(str(target))
    targets,path_registry,remembered_paths=mount_project_paths(client,aliases,previous_targets)
    if not recover:remove_mounts({'clientId':client,'syncProjects':False})
    managed=Path('/mnt/safe-code');secure_root_directory(managed,0o755);managed=managed/a['username'];secure_root_directory(managed,0o755);managed=managed/client;secure_root_directory(managed,0o755)
    existing_mounts={line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()}
    if recover and (not previous or previous.get('aliases')!=aliases or not set(previous.get('paths',[])).issubset(set(targets))):raise RuntimeError('原挂载记录不一致，已保留现有挂载，请检查后重新保存。')
    for target in targets:
        if target not in existing_mounts:secure_root_directory(target,0o755)
    if not Path('/dev/fuse').exists():run(['modprobe','fuse'])
    directory=STATE/('mount-'+client);directory.mkdir(mode=0o700,exist_ok=True)
    config=directory/'davfs2.conf'
    write(config,'use_locks 0\ndelay_upload 0\ncache_size 1024\nconnect_timeout 10\nread_timeout 120\nretry 5\nmax_retry 30\nmax_upload_attempts 1000000\n',0o600)
    uid=pwd.getpwnam(a['username']).pw_uid;gid=pwd.getpwnam(a['username']).pw_gid
    urls=['http://127.0.0.1:'+str(port)+'/m'+str(i)+'/' for i in range(len(targets))]
    if recover and previous.get('urls')!=urls:raise RuntimeError('原挂载通道已变化，未卸载现有目录，请人工检查。')
    if not recover:
        path=Path('/etc/davfs2/secrets');old=path.read_text() if path.exists() else ''
        write(path,old.rstrip()+'\n# safe-code-'+client+'\n'+'\n'.join(url+' '+user+' '+password for url in urls)+'\n',0o600)
    record=STATE/('mount-'+client+'.json')
    if not recover:write(record,json.dumps({'paths':[],'urls':urls,'aliases':aliases}),0o600)
    mounted=[x for x in targets if x in existing_mounts] if recover else []
    try:
        for target,url,alias in zip(targets,urls,aliases):
            if target in mounted:continue
            target=Path(target)
            log('建立加密文件通道：'+str(target))
            try:completed=subprocess.run(['mount','-t','davfs',url,str(target),'-o','conf='+str(config)+',uid='+str(uid)+',gid='+str(gid)+',file_mode=0600,dir_mode=0700,noexec,nosuid,nodev'],input='',text=True,capture_output=True,timeout=75)
            except subprocess.TimeoutExpired:raise OperationError('mount_timeout','建立目录挂载超时，请检查 SSH 文件通道后重试。')
            if completed.returncode:raise OperationError('mount_failed','目录挂载失败：'+completed.stderr[-800:])
            if not os.path.ismount(target):raise RuntimeError('目标未成为实际挂载点。')
            mounted.append(str(target));write(record,json.dumps({'paths':[x for x in targets if x in mounted],'urls':urls,'aliases':aliases}),0o600)
            user_files('alias',str(Path(alias).relative_to(home())),destination=str(target))
        # Keep legacy m0/m1 paths after unmount too, so upgrading retains IDs.
        write(path_registry,json.dumps(remembered_paths),0o600)
        warning=sync_cloud_mount_projects()
        return {'clientId':client,'paths':aliases,'managedPaths':mounted,'cloudWarning':warning}
    except Exception:
        if not recover:remove_mounts({'clientId':client})
        raise

def start_job(request):
    operation=request.get('op')
    if operation not in LONG_OPERATIONS:raise RuntimeError('此操作不能作为后台安装任务。')
    job=secrets.token_hex(16);directory=STATE/'jobs'/job;directory.mkdir(mode=0o700,parents=True)
    request={**request,'background':False,'jobId':job}
    options=request.get('options') or {}
    if operation=='claude' and options.get('claudePayload'):
        source=Path(options['claudePayload'])
        if source.parent!=Path(__file__).resolve().parent or source.is_symlink() or not re.fullmatch('claude-payload-[a-f0-9]{32}',source.name):raise RuntimeError('Claude Code 安装文件路径无效。')
        target=directory/source.name;shutil.copyfile(source,target);target.chmod(0o600)
        request={**request,'options':{**options,'claudePayload':str(target)}}
    write(directory/'request.json',json.dumps(request),0o600)
    write(directory/'metadata.json',json.dumps({'jobId':job,'operation':operation,'created':time.time()}),0o600)
    for name in ['agent.py','network.py']:
        shutil.copy2(Path(__file__).parent/name,directory/name);(directory/name).chmod(0o600)
    write(directory/'output.log','',0o600)
    run(['systemd-run','--quiet','--collect','--unit=safe-code-job-'+job,'--property=Type=exec','--property=StandardOutput=append:'+str(directory/'output.log'),'--property=StandardError=append:'+str(directory/'output.log'),'/usr/bin/python3','-u',str(directory/'agent.py'),str(directory/'request.json')])
    return {'jobId':job,'operation':operation}

def job_directory(job):
    if not re.fullmatch('[a-f0-9]{32}',job):raise RuntimeError('后台任务标识无效。')
    directory=STATE/'jobs'/job
    if not directory.is_dir():raise RuntimeError('后台任务记录不存在。')
    return directory

def active_jobs():
    root=STATE/'jobs'
    if not root.exists():return []
    values=[]
    for directory in sorted(root.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True)[:30]:
        if directory==JOB_DIR or not (directory/'metadata.json').exists() or (directory/'result.json').exists() or (directory/'error.json').exists():continue
        metadata=json.loads((directory/'metadata.json').read_text())
        status=run(['systemctl','is-active','safe-code-job-'+directory.name+'.service'],False).stdout.strip()
        if status in ['active','activating']:values.append({'jobId':directory.name,'operation':metadata['operation']})
    return values

def poll_job(job,offset):
    directory=job_directory(job);offset=max(0,int(offset));lines=[];next_offset=offset
    path=directory/'output.log'
    if path.exists():
        with path.open('rb') as f:
            f.seek(offset);data=f.read(131072)
        complete=data.rfind(b'\n')+1
        if complete:
            data=data[:complete];next_offset=offset+len(data)
            lines=[x for x in data.decode('utf-8',errors='replace').splitlines() if not x.startswith(('SC_RESULT:','SC_ERROR:','SC_ERROR_JSON:','SC_ERROR_CODE:'))]
    value=json.loads((directory/'result.json').read_text()) if (directory/'result.json').exists() else None
    failure=json.loads((directory/'error.json').read_text()) if (directory/'error.json').exists() else {}
    error=failure.get('error')
    if value is None and error is None:
        status=run(['systemctl','is-active','safe-code-job-'+job+'.service'],False).stdout.strip()
        age=time.time()-json.loads((directory/'metadata.json').read_text())['created']
        if status not in ['active','activating'] and age>3:error='远端任务意外结束，重连后可检查实际安装状态。'
    return {'lines':lines,'nextOffset':next_offset,'done':value is not None or error is not None,'value':value,'error':error,'errorCode':failure.get('code')}

def main():
    global JOB_DIR
    require_ubuntu() # 必须先检查系统，再创建任何目录或写入配置。
    if sys.argv[1]=='--claude-download':
        if os.geteuid()!=0:raise RuntimeError('需要安装管理员权限。')
        result(download_claude());return
    if sys.argv[1]=='--list-directories':
        if os.geteuid()!=0:raise RuntimeError('需要安装管理员权限。')
        result(list_directories(json.loads(base64.b64decode(sys.argv[2]))));return
    if sys.argv[1]=='--poll':
        if os.geteuid()!=0:raise RuntimeError('需要安装管理员权限。')
        result(poll_job(sys.argv[2],sys.argv[3]));return
    request=json.loads(Path(sys.argv[1]).read_text())
    if request.get('jobId'):JOB_DIR=job_directory(request['jobId'])
    op=request.get('op','inspect')
    if op=='inspect':result(inspect());return
    if op=='egress_probe':
        value=run(['curl','--noproxy','*','-4','-fsS','--connect-timeout','5','--max-time','12','https://api.ipify.org']).stdout.strip()
        result({'ip':str(ipaddress.IPv4Address(value))});return
    if os.geteuid()!=0:raise RuntimeError('安装需要 root 或免密 sudo 权限。')
    if op=='account_info':result(account());return
    if op=='list_directories':result(list_directories(request.get('options') or {}));return
    if op=='create_directory':result(create_directory(request.get('options') or {}));return
    if op=='desktop_login_probe':
        options=request.get('options') or {};path=Path('/var/log/xrdp-sesman.log');size=path.stat().st_size
        offset=options.get('offset',size)
        if not isinstance(offset,int) or offset<0 or offset>size:offset=size
        with path.open('rb') as f:
            f.seek(offset);lines=f.read(131072).decode('utf-8',errors='replace')
        user=account()['username']
        success=any(('++ created session' in line or '++ reconnected session' in line or 'Session started successfully' in line) and (user in line) for line in lines.splitlines())
        result({'offset':size,'loggedIn':success});return
    secure_root_directory(STATE,0o700)
    write('/etc/needrestart/conf.d/99-safe-code.conf', '$nrconf{override_rc}->{qr(^(?:ssh|xrdp(?:-sesman)?)(?:\\.service)?$)} = 0;\n')
    secure_root_directory('/opt/safe-code',0o755)
    if request.get('background'):
        result(start_job(request));return
    with (STATE/'install.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise OperationError('deployment_busy','另一个部署正在运行，请等待完成。')
        options=request.get('options') or {}
        if options.get('password'):REDACTIONS.append(options['password'])
        if op.startswith('ip_'):
            import network
            if op=='ip_prepare':
                apt(['nftables','curl']);result(network.prepare(options));return
            if op=='ip_apply':result(network.schedule_apply(options['token']));return
            if op=='ip_commit':result(network.commit(options['token']));return
            if op=='ip_rollback':result(network.rollback(options['token']));return
            raise RuntimeError('未知网络操作。')
        if op=='codex_bridge':result(codex_bridge(options));return
        if op=='host_bridge':result(host_bridge(options));return
        if op=='mount_apply':result(apply_mounts(options));return
        if op=='mount_recover':result(apply_mounts(options,recover=True));return
        if op=='mount_remove':result(remove_mounts(options));return
        mapping={'account':setup_account,'localization':setup_locale,'desktop':setup_desktop,'chrome':setup_chrome,'input':setup_input,'claude':setup_claude,'ssh':setup_ssh,'mount_install':install_mount,'cloud':setup_cloud,'cloud_public':setup_cloud_public,'cloud_setup':setup_cloud_onboarding}
        if op not in mapping:raise RuntimeError('未知部署操作。')
        log('开始部署：'+op)
        if (op=='account' and not read_config()) or op in ['localization','desktop','chrome','input','claude','cloud','mount_install']:prepare_system()
        mapping[op](options)
        result(inspect())

if __name__=='__main__':
    try:main()
    except Exception as error:
        message=str(error)
        for secret in REDACTIONS:
            if secret:message=message.replace(secret,'[已隐藏]')
        if JOB_DIR is not None:write(JOB_DIR/'error.json',json.dumps({'error':message,'code':error_code(error)},ensure_ascii=False),0o600)
        print('SC_ERROR_CODE:'+error_code(error),flush=True)
        print('SC_ERROR_JSON:'+json.dumps(message,ensure_ascii=False),flush=True);sys.exit(1)
