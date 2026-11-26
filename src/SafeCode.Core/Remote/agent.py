#!/usr/bin/env python3
"""Safe Code Ubuntu 部署端。僅由客戶端顯式請求執行；不包含服務憑據。"""
import base64,contextlib,fcntl,hashlib,ipaddress,json,os,pwd,re,secrets,shlex,shutil,subprocess,sys,time,tempfile
from pathlib import Path
STATE=Path('/var/lib/safe-code')
SECRETS_PATH=Path('/etc/davfs2/secrets')
REDACTIONS=[]
JOB_DIR=None
class OperationError(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code

ERROR_CODES={'json_invalid','access_denied','timeout','operation_failed','invalid_argument','deployment_busy','mount_failed','mount_timeout','mount_target_invalid','package_lock_wait','package_fetch_failed','dns_failure','disk_full','package_interrupted'}

def error_code(error):
    code=getattr(error,'code',None)
    if code in ERROR_CODES:return code
    if isinstance(error,json.JSONDecodeError):return 'json_invalid'
    if isinstance(error,PermissionError):return 'access_denied'
    if isinstance(error,subprocess.TimeoutExpired):return 'timeout'
    return 'operation_failed'

LONG_OPERATIONS={'account','localization','desktop','chrome','input','claude','ssh','cloud','mount_install','safe_code_api_repair'}
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
        raise RuntimeError('無法安裝環境：僅支援 Ubuntu，當前系統為 '+info.get('PRETTY_NAME','未知系統'))
    return info

def result(value):
    if JOB_DIR is not None:write(JOB_DIR/'result.json',json.dumps(value,ensure_ascii=False),0o600)
    print('SC_RESULT:'+json.dumps(value,ensure_ascii=False),flush=True)
def log(message):
    message=str(message)
    for secret in REDACTIONS:
        if secret:message=message.replace(secret,'[已隱藏]')
    line=time.strftime('%H:%M:%S')+' '+message
    print(line,flush=True)
    if STATE.exists():
        with (STATE/'events.log').open('a') as f:f.write(line+'\n')

def run(args,check=True,env=None,stdin=None,timeout=180):
    p=subprocess.run(args,text=True,capture_output=True,input=stdin,env=env,timeout=timeout)
    if check and p.returncode:
        detail=p.stderr or p.stdout or ''
        if 'curl'==Path(args[0]).name:
            host=next((x.split('/')[2] for x in args if isinstance(x,str) and x.startswith('https://')),'下載服務')
            if p.returncode in (5,6) or 'resolving timed out' in detail.lower():
                raise OperationError('dns_failure','無法連接 '+host+'：DNS 域名解析失敗，請檢查伺服器 DNS 或網絡後重試。')
            raise OperationError('package_fetch_failed','無法從 '+host+' 下載檔案（狀態碼 '+str(p.returncode)+'），請檢查網絡後重試。')
        raise RuntimeError(detail[-1600:] or '命令執行失敗')
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
        hints={'package_lock_wait':'系統包管理器正被佔用，請稍後重試。','dns_failure':'伺服器無法解析下載域名，請檢查 DNS 後重試。','package_fetch_failed':'軟件包下載失敗，請檢查伺服器網絡後重試。','disk_full':'伺服器磁碟空間不足，請清理後重試。','package_interrupted':'上次軟件包安裝被中斷，請修復 dpkg 後重試。'}
        hint=hints.get(reason,'可能是系統正在更新，請稍後重試；如果仍有問題，請提交回饋。' if p.returncode==100 else '請提交回饋。')
        raise OperationError(reason or 'operation_failed','安裝命令退出，狀態碼 '+str(p.returncode)+'。'+hint)

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
    if not value:raise RuntimeError('請先部署工作用戶。')
    return value

def home():return Path(pwd.getpwnam(account()['username']).pw_dir)
USER_FILES = r"""
import json,os,pathlib,sys,tempfile
request=json.load(sys.stdin);base=pathlib.Path(os.environ['HOME']).resolve();relative=pathlib.Path(request['relative'])
if relative.is_absolute() or '..' in relative.parts:raise ValueError('用戶目錄路徑無效。')
target=base/relative;operation=request['operation']
checked=target.parent.resolve() if operation in ['alias','check_alias','remove_alias'] else target.resolve()
if not checked.is_relative_to(base):raise ValueError('用戶配置路徑指向主目錄之外，已拒絕操作。')
os.umask(0o077)
if operation=='mkdir':
    if not target.parent.is_dir():raise ValueError('上級資料夾不存在，請重新選擇。')
    try:target.mkdir(mode=0o700)
    except FileExistsError:raise ValueError('同名檔案或資料夾已存在，請換一個名稱。')
    print(json.dumps({'path':str(target),'name':target.name}));sys.exit(0)
if operation=='cloud_onboarding':
    import sqlite3
    if not target.is_file():raise ValueError('CloudCLI 網頁資料庫尚未初始化，請稍後重試。')
    connection=sqlite3.connect(str(target),timeout=15)
    try:
        required={'id','git_name','git_email','has_completed_onboarding','is_active'}
        if not required.issubset({row[1] for row in connection.execute('PRAGMA table_info(users)')}):raise ValueError('CloudCLI 初始化數據結構不相容，未修改帳號。')
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
        if not required.issubset({row[1] for row in connection.execute('PRAGMA table_info(projects)')}):raise ValueError('CloudCLI 項目數據結構不相容，未修改項目。')
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
    if not re.fullmatch('[a-f0-9]{20}',client) or relative!=pathlib.Path('.ssh/safe-code-host/'+client+'/identity'):raise ValueError('本機控制標識無效。')
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
    if action not in ('configure','verify'):raise ValueError('本機控制操作無效。')
    alias=request['alias'];username=request['username'];port=request['port'];host_key=request['hostKey'];user_sid=request.get('userSid','')
    if not re.fullmatch('[a-z][a-z0-9_-]{0,31}',alias) or not re.fullmatch(r'[a-zA-Z0-9_.@\\-]{1,128}',username):raise ValueError('本機控制別名或用戶名無效。')
    is_mac=bool(re.fullmatch(r'macos:[1-9][0-9]{0,9}',user_sid))
    if not is_mac and not re.fullmatch(r'S-1-[0-9]+(?:-[0-9]+){1,15}',user_sid):raise ValueError('本機帳戶標識無效。')
    if not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('本機控制連接埠無效。')
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?',host_key):raise ValueError('本機 SSH 指紋材料無效。')
    if not target.is_file():raise ValueError('缺少專用連線密鑰，請重新部署本機控制。')
    if config.exists() and not config.read_text().startswith(marker):raise ValueError('本機控制配置被修改，未覆蓋。')
    host_alias='safe-code-host-'+client
    body=marker+'Host '+alias+'\n  HostName 127.0.0.1\n  Port '+str(port)+'\n  User '+username+'\n  IdentityFile '+str(target)+'\n  IdentitiesOnly yes\n  BatchMode yes\n  PasswordAuthentication no\n  KbdInteractiveAuthentication no\n  StrictHostKeyChecking yes\n  HostKeyAlias '+host_alias+'\n  UserKnownHostsFile '+str(known)+'\n  GlobalKnownHostsFile /dev/null\n  ForwardAgent no\n  ClearAllForwardings yes\n  ProxyCommand none\n  ProxyJump none\n  PermitLocalCommand no\n  ConnectTimeout 10\n  ServerAliveInterval 15\n  ServerAliveCountMax 2\n'
    def atomic(path,value):
        with tempfile.NamedTemporaryFile('w',dir=directory,delete=False) as f:f.write(value);temporary=pathlib.Path(f.name)
        temporary.chmod(0o600);os.replace(temporary,path)
    pin=host_alias+' '+' '.join(host_key.split()[:2])+'\n'
    if action=='configure':
        atomic(known,pin);atomic(config,body)
        completed=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'whoami'],capture_output=True,text=True,timeout=20)
        if completed.returncode:raise ValueError('本機 SSH 登入驗證失敗，請檢查本機帳戶、OpenSSH 日誌與授權。')
        identity=completed.stdout.strip().lower().replace('/','\\');expected=username.lower()
        if identity!=expected and not identity.endswith('\\'+expected):raise ValueError('本機登入帳戶不匹配，已拒絕生成可用狀態。')
    elif config.is_symlink() or known.is_symlink() or config.read_text()!=body or known.read_text()!=pin:
        raise ValueError('本機控制配置或指紋已變化。')
    if is_mac:
        proof=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'/usr/bin/uname -s && /usr/bin/id -u'],capture_output=True,text=True,timeout=8 if action=='verify' else 20)
        if proof.returncode or proof.stdout.strip().splitlines()!=['Darwin',user_sid.split(':',1)[1]]:raise ValueError('本機 macOS 帳戶身份與授權不匹配，未生成可用狀態。')
    else:
        import base64
        probe="$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
        encoded=base64.b64encode(probe.encode('utf-16le')).decode()
        proof=subprocess.run(['/usr/bin/ssh','-F',str(config),alias,'powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],capture_output=True,text=True,errors='replace',timeout=20)
        if proof.returncode or proof.stdout.strip()!=user_sid:raise ValueError('本機 Windows 帳戶身份與授權不匹配，未生成可用狀態。')
    import shlex
    print(json.dumps({'command':'ssh -F '+shlex.quote(str(config))+' '+alias,'verified':True}));sys.exit(0)
if operation=='codex_bridge':
    import base64,fcntl,hashlib,re,shlex,socket,struct,subprocess,time
    if relative!=pathlib.Path('.local/state/safe-code/codex-bridge'):raise ValueError('適配器管理路徑無效。')
    key=request.get('publicKey','').strip();parts=key.split();key_id=request.get('keyId','');client=request.get('clientId','')
    if len(parts)<2 or parts[0] not in ['ssh-rsa','ssh-ed25519'] or not re.fullmatch('[A-Za-z0-9+/=]{40,2048}',parts[1]):raise ValueError('適配器公鑰無效。')
    if key_id!=hashlib.sha256(parts[1].encode()).hexdigest()[:20] or not re.fullmatch(r'c_[a-f0-9]{24}',client):raise ValueError('適配器客戶端標識無效。')
    action=request['action'];state=target;marker=state/'safe-code-component.json';ssh=base/'.ssh';auth=ssh/'authorized_keys'
    if action not in ['install','remove']:raise ValueError('適配器操作無效。')
    for path in [state,ssh,auth]:
        if not path.resolve().is_relative_to(base):raise ValueError('適配器配置路徑越界，已拒絕。')
    if auth.is_symlink():raise ValueError('SSH 授權檔案為連結，未修改。')
    if state.exists() and any(state.iterdir()) and not marker.is_file():raise ValueError('適配器狀態目錄已有未知內容，未覆蓋。')
    if action=='remove' and not state.exists():print(json.dumps({'ready':False,'warning':None}));sys.exit(0)
    if marker.exists() and (marker.is_symlink() or json.loads(marker.read_text()).get('managedBy')!='safe-code'):raise ValueError('適配器目錄標記不屬於 Safe Code，未覆蓋。')
    state.mkdir(mode=0o700,parents=True,exist_ok=True);state.chmod(0o700)
    def atomic(path,content,mode=0o600):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if not path.parent.resolve().is_relative_to(base) or path.is_symlink():raise ValueError('適配器檔案路徑異常，未寫入。')
        with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as f:f.write(content);tmp=pathlib.Path(f.name)
        tmp.chmod(mode);os.replace(tmp,path)
    if action=='install' and not marker.exists():atomic(marker,json.dumps({'managedBy':'safe-code','sourceHash':request['sourceHash']}))
    owner=state/'clients'/(key_id+'.json');tag='safe-code-codex-'+key_id;warning=None
    with (state/'install.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=auth.read_text() if auth.exists() else ''
        saved=json.loads(owner.read_text()) if owner.exists() else None
        if saved and (saved.get('body')!=parts[1] or saved.get('clientId')!=client):raise ValueError('適配器授權記錄不匹配。')
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
                            if not chunk:raise OSError('組件連線已結束。')
                            data+=chunk
                        return data
                    def frame():
                        head,body=struct.unpack('>II',read_n(8))
                        if head>16*1024*1024 or body>4*1024*1024:raise ValueError('適配器狀態幀過大。')
                        value=json.loads(read_n(head));read_n(body);return value
                    try:
                        conn.connect(str(sock_path));send({'op':'hello','role':'client','v':1,'clientId':client,'resume':[]})
                        hello=frame()
                        if hello.get('op')!='hello.ok':raise ValueError('適配器未確認客戶端狀態。')
                        pending={t['turnId'] for t in hello.get('turns',[]) if not t.get('exited')}
                        for turn in pending:send({'op':'turn.cancel','turnId':turn,'reason':'safe-code-stop'})
                        deadline=time.monotonic()+12
                        while pending and time.monotonic()<deadline:
                            event=frame()
                            if event.get('op')=='turn.exit':pending.discard(event.get('turnId'))
                        if pending:warning='本機入口與專用授權已關閉，部分遠端任務尚未確認結束。'
                    except Exception:warning='本機入口與專用授權已關閉；遠端任務停止未確認，需重連後重試清理。'
                    finally:conn.close()
                if not warning:owner.unlink(missing_ok=True)
            print(json.dumps({'ready':False,'warning':warning}));sys.exit(0)
        if action!='install':raise ValueError('適配器操作無效。')
        digest=request['sourceHash'];node=request['node'];claude=request['claude']
        if not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('適配器版本資訊無效。')
        for executable in [node,claude]:
            if not pathlib.Path(executable).is_absolute() or not os.access(executable,os.X_OK):raise ValueError('適配器執行環境不完整，請先部署 Claude Code。')
        help_result=subprocess.run([claude,'--help'],capture_output=True,text=True,errors='replace',timeout=20)
        if help_result.returncode or not all(flag in help_result.stdout+help_result.stderr for flag in ['--tools','--strict-mcp-config']):raise ValueError('Claude Code 不支援所需工具限制，請先更新 Claude Code。')
        if marker.exists() and json.loads(marker.read_text()).get('sourceHash')!=digest and (state/'run/daemon.sock').exists():raise ValueError('舊適配器組件仍在使用，請先結束相關會話再更新。')
        library=base/'.local/share/safe-code/codex-bridge'/digest[:16]
        if not library.resolve().is_relative_to(base):raise ValueError('適配器程式目錄越界。')
        for name,encoded in request['files'].items():
            rel=pathlib.Path(name)
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('適配器檔案名無效。')
            destination=library/rel;data=base64.b64decode(encoded)
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes()!=data:raise ValueError('適配器程式已有不同內容，未覆蓋。')
            else:atomic(destination,data.decode('utf-8-sig'))
        wrapper=state/'bin/connect';command=shlex.quote(str(wrapper))
        script='#!/bin/sh\nif [ "${SSH_ORIGINAL_COMMAND:-}" = "ccw-ping" ]; then echo codex-claude-win-ok; exit 0; fi\nunset SSH_CLIENT SSH_CONNECTION SSH_TTY SSH_AUTH_SOCK SSH_ORIGINAL_COMMAND SSH_USER_AUTH\nexport PATH='+shlex.quote(str(pathlib.Path(node).parent)+':'+str(base/'.local/bin')+':/usr/local/bin:/usr/bin:/bin')+'\nexport CODEX_CLAUDE_WIN_HOME='+shlex.quote(str(state))+'\nexec '+shlex.quote(node)+' '+shlex.quote(str(library/'src/vps/connect.mjs'))+'\n'
        atomic(wrapper,script,0o700)
        policy={'claudePath':claude,'allowedNativeTools':['none','web'],'maxBlobCacheBytes':268435456,'detachGraceMs':30000,'maxConcurrentTurns':4}
        policy_file=state/'policy.json'
        if not policy_file.exists():atomic(policy_file,json.dumps(policy))
        else:
            existing=json.loads(policy_file.read_text())
            if existing.get('claudePath')!=claude or set(existing.get('allowedNativeTools',[]))-{'none','web'}:raise ValueError('適配器策略已被修改，未自動覆蓋。')
        ssh.mkdir(mode=0o700,exist_ok=True);ssh.chmod(0o700)
        line='restrict,command="'+command.replace('\\','\\\\').replace('"','\\"')+'" '+parts[0]+' '+parts[1]+' '+tag
        matches=[item for item in previous.splitlines() if parts[1] in item.split()]
        if matches and any(item!=line for item in matches):raise ValueError('此公鑰已有不同授權，未放寬或覆蓋。')
        if not matches:
            if previous and not (state/'authorized-keys-before-first-install').exists():atomic(state/'authorized-keys-before-first-install',previous)
            atomic(auth,previous.rstrip()+'\n'+line+'\n')
        atomic(owner,json.dumps({'body':parts[1],'clientId':client}));atomic(marker,json.dumps({'managedBy':'safe-code','sourceHash':digest}))
        print(json.dumps({'ready':True,'username':request['username'],'connectCommand':command,'warning':None}));sys.exit(0)
if operation=='install_claude':
    import shutil
    directory=base/'.local/share/claude/versions'
    if not directory.resolve().is_relative_to(base):raise ValueError('Claude Code 安裝路徑指向主目錄之外。')
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
if operation in ('gateway_origin', 'gateway_key_host_reuse'):
    import hashlib, hmac, re, ssl
    key_id = request['keyId']
    if not isinstance(key_id, str) or not re.fullmatch(r'[a-f0-9]{64}', key_id) or not (target/'keys'/key_id).is_file():
        if operation == 'gateway_key_host_reuse':
            print(json.dumps({'allowed': False}));sys.exit(0)
        raise ValueError('這把 Key 在當前 VPS 上不存在或已撤銷。')
    with (target/'cert.pem').open() as cert_file:
        cert = ssl.PEM_cert_to_DER_cert(cert_file.read(65536))
    pin = hashlib.sha256(cert).hexdigest()
    if operation == 'gateway_key_host_reuse':
        supplied_pin = request.get('pin')
        allowed = (isinstance(supplied_pin, str) and re.fullmatch(r'[a-f0-9]{64}', supplied_pin)
                   and hmac.compare_digest(pin, supplied_pin) and not (target/'device-required'/key_id).is_file())
        print(json.dumps({'allowed': bool(allowed)}));sys.exit(0)
    print(json.dumps({'pin':pin}));sys.exit(0)
if operation=='read':
    if not target.is_file():print(json.dumps(''));sys.exit(0)
    with target.open() as f:value=f.read(1048577)
    if len(value)>1048576:raise ValueError('用戶設定檔過大。')
    print(json.dumps(value));sys.exit(0)
if operation in ['alias','check_alias']:
    destination=pathlib.Path(request['destination'])
    if target.is_symlink():
        if target.readlink()!=destination:raise ValueError('目標已存在其他符號連結，未覆蓋。')
        sys.exit(0)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):raise ValueError('伺服器目錄不是空目錄，請選擇新的或空的目錄：'+str(target))
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
    if target.exists() and (not target.is_file() or target.stat().st_size>1048576):raise ValueError('設定檔不是普通檔案或超過大小限制。')
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
    if entry.pw_uid==0:raise RuntimeError('工作帳戶不能是 root。')
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
    except subprocess.TimeoutExpired:raise OperationError('timeout','工作用戶目錄操作超時，請檢查伺服器連線或掛載狀態後重試。')
    if completed.returncode:
        detail=(completed.stderr or completed.stdout or '用戶目錄操作失敗').strip().splitlines()[-1]
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
        if parent.is_symlink() or data.st_uid!=0 or data.st_mode&0o022:raise RuntimeError('程式管理目錄的上級路徑權限不安全：'+str(parent))
    if path.exists() or path.is_symlink():
        data=path.lstat()
        if path.is_symlink() or not path.is_dir() or data.st_uid!=0 or data.st_mode&0o022:raise RuntimeError('程式管理目錄權限不安全，未繼續安裝：'+str(path))
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
            raise OperationError('package_lock_wait','系統更新仍在執行，已等待 15 分鐘。請等待其完成後重試；不會強行停止更新或刪除鎖檔案。')
        log('系統正在更新，正在等待包管理器完成，請勿關閉伺服器…')
        time.sleep(15)

def prepare_system():
    marker=STATE/'system-prepared.json'
    if marker.exists():return
    log('安裝前準備：先等待系統更新完成，再更新軟件源和系統軟件包。')
    wait_package_manager()
    env=package_environment();opts=package_options()
    audit=run(['dpkg','--audit'])
    if audit.stdout.strip():
        log('檢測到未完成的軟件包配置，正在繼續完成…')
        stream(['dpkg','--configure','-a'],env)
    log('正在更新軟件源，請稍候…');stream(opts+['update'],env)
    log('正在更新系統軟件包，首次安裝可能需要較長時間…')
    stream(opts+['upgrade','-y','--with-new-pkgs','--no-remove'],env)
    write(marker,json.dumps({'completedAt':int(time.time())}),0o600)
    log('系統準備完成，繼續安裝所選組件。')
    if Path('/var/run/reboot-required').exists():log('系統更新提示稍後需要重新啟動；本次不會自動重新啟動伺服器。')

def apt(packages):
    missing=[p for p in packages if not package_installed(p)]
    if not missing:log('所需組件已安裝，跳過重複下載。');return
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
        # 用戶可修改的程式絕不能在管理員巡檢時以 root 執行。
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
    socks=None
    if (STATE/'socks-egress.json').exists():
        import socks_egress
        socks=socks_egress.status()
    language=Path('/etc/default/locale').read_text() if Path('/etc/default/locale').exists() else ''
    match=re.search(r'(?m)^LANG="?([^\s"]+)',language)
    return {'osId':info.get('ID',''),'osName':info.get('PRETTY_NAME',''),'osVersion':info.get('VERSION_ID',''),
      'architecture':os.uname().machine,'memoryBytes':mem,'diskFreeBytes':disk.free,'cpuCount':os.cpu_count(),
      'hostname':os.uname().nodename, 'homeDirectory':str(h), 'isRoot':os.geteuid()==0,'username':a['username'] if account_ok else '', 'statuses':statuses,
      'assignedIps':addresses,'timezone':run(['timedatectl','show','-p','Timezone','--value'],False).stdout.strip(),
      'systemLocale':match[1] if match else '', 'ipPolicyHealthy':healthy,
      'claudeVersion':version(h/'.local/bin/claude'),'desktopReady':statuses['desktop'],
      'cloudReady':active('safe-code-cloudcli'),'mountReady':package_installed('davfs2'),
      'ipPolicy':policy,'socksEgress':socks,'activeJobs':active_jobs() if os.geteuid()==0 else []}

def setup_account(options):
    existing=read_config()
    user=options.get('username') or (existing['username'] if existing else 'workspace')
    hostname=options.get('hostname','').strip() or (existing or {}).get('hostname','') or 'safe-'+secrets.token_hex(4)
    password=options.get('password','')
    if not re.fullmatch('[a-z][a-z0-9_-]{1,30}',user):raise RuntimeError('用戶名須為 2–31 位小寫字母、數字、下劃線或短橫線。')
    if hostname and not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',hostname):raise RuntimeError('機器名稱須為 1–63 位字母、數字或短橫線。')
    if password and (len(password)<12 or len(password)>256 or any(c in password for c in '\r\n\0')):raise RuntimeError('工作密碼須為 12–256 位，不能包含換行或空字符。')
    if existing:
        if run(['id',existing['username']],False).returncode:raise RuntimeError('工作用戶記錄與系統不一致，請手動檢查。')
        if user!=existing['username']:
            # 不移動或覆蓋已有帳戶，建立獨立環境，舊項目完整保留。
            uid=pwd.getpwnam(existing['username']).pw_uid
            if run(['pgrep','-u',str(uid),'-x','xfce4-session'],False).returncode==0:
                raise RuntimeError('請先在原工作用戶的遠端桌面中登出，再更換用戶名；現有帳戶、項目和會話均未修改。')
            if any(os.path.ismount(p) for p in Path('/mnt/safe-code').glob('*/*/*')):
                raise RuntimeError('更換用戶名需要先卸載目錄；原帳戶與項目不會被刪除。')
        else:
            # 先完成不會修改密碼的操作，避免機器名設定失敗後密碼已悄悄改變。
            run(['hostnamectl','set-hostname',hostname])
            if password:
                run(['chpasswd'],stdin=user+':'+password+'\n')
            try:write(STATE/'account.json',json.dumps({'username':user,'password':password or existing['password'],'hostname':hostname}),0o600)
            except Exception as error:
                if password:raise RuntimeError('工作用戶密碼已更新，但帳戶記錄儲存失敗。請使用剛設定的新工作密碼；伺服器登入帳戶密碼未修改。修復配置目錄權限或磁碟空間後，可重新應用帳戶設定。') from error
                raise
            log('工作帳戶設定已應用。');return
    if run(['id',user],False).returncode==0:raise RuntimeError('此用戶已存在，拒絕覆蓋其密碼。請選擇新工作用戶名。')
    if existing and Path('/etc/systemd/system/safe-code-cloudcli.service').exists():
        run(['systemctl','disable','--now','safe-code-cloudcli'])
        log('舊工作用戶的 CloudCLI 已停止；可為新用戶重新安裝。')
    password=password or secrets.token_urlsafe(20)
    run(['hostnamectl','set-hostname',hostname])
    run(['useradd','-m','-s','/bin/bash',user])
    run(['chpasswd'],stdin=user+':'+password+'\n')
    try:write(STATE/'account.json',json.dumps({'username':user,'password':password,'hostname':hostname}),0o600)
    except Exception as error:raise RuntimeError('工作用戶已建立並設定密碼，但帳戶記錄儲存失敗。請保留剛設定的工作用戶名和密碼，透過伺服器控制台檢查配置目錄權限或磁碟空間；伺服器登入帳戶密碼未修改。') from error
    user_write('workspace/.keep','')
    if package_installed('xfce4-session'):setup_xsession()
    log('工作帳戶、機器名稱與項目目錄已建立。')

def list_directories(options):
    a=account();entry=pwd.getpwnam(a['username'])
    # 瀏覽以普通工作用戶身份執行，不洩露管理員專屬目錄。
    script="""import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve(strict=True)
if not p.is_dir():raise ValueError('目錄不存在')
items=[]
for child in os.scandir(p):
 try:
  if child.is_dir(follow_symlinks=False) or child.is_symlink():items.append({'name':child.name,'path':str(p/child.name)})
 except OSError:pass
 if len(items)>=300:break
print(json.dumps({'path':str(p),'parent':str(p.parent),'directories':sorted(items,key=lambda x:x['name'].casefold()),'truncated':len(items)>=300}))
"""
    path=options.get('path') or str(Path(entry.pw_dir)/'workspace')
    if not isinstance(path,str) or len(path)>4096 or '\0' in path:raise RuntimeError('目錄路徑無效。')
    log('以工作用戶讀取目錄：'+path)
    try:
        output=subprocess.run(['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','python3','-I','-c',script,path],capture_output=True,text=True,timeout=8)
    except subprocess.TimeoutExpired:raise RuntimeError('讀取目錄超時，請檢查網絡或失效的掛載目錄後重試。')
    if output.returncode:
        if 'PermissionError' in output.stderr:raise RuntimeError('工作用戶沒有讀取該目錄的權限，請選擇其他目錄。')
        if 'FileNotFoundError' in output.stderr:raise RuntimeError('目錄不存在，請返回上一級或填寫其他目錄。')
        raise RuntimeError('無法讀取該目錄，請檢查目錄狀態後重試。')
    log('目錄讀取完成。')
    return json.loads(output.stdout)

def create_directory(options):
    name=options.get('name','').strip();parent=options.get('path','') or str(home()/'workspace')
    if not name or len(name)>128 or name in ('.','..') or any(c in name for c in '/\\\0\r\n'):raise RuntimeError('資料夾名稱須為 1–128 個字符，不能包含路徑分隔符或換行。')
    target=Path(parent)/name
    if not Path(parent).is_absolute() or '..' in Path(parent).parts or not target.is_relative_to(home()):raise RuntimeError('請在工作用戶主目錄內新建資料夾。')
    return json.loads(user_files('mkdir',str(target.relative_to(home()))).stdout)

def locale_packages(locale):
    language=locale.split('_',1)[0]
    if not re.fullmatch('[a-z]{2}',language):raise RuntimeError('系統語言無效。')
    if language=='zh':language='zh-hant' if locale.startswith(('zh_TW','zh_HK','zh_MO')) else 'zh-hans'
    return ['language-pack-'+language,'language-pack-gnome-'+language]

def setup_locale(options):
    account()
    timezone=options.get('timezone','');locale=options.get('locale','')
    if not timezone or '..' in timezone or not (Path('/usr/share/zoneinfo')/timezone).is_file():raise RuntimeError('請選擇有效的 IANA 時區。')
    if not re.fullmatch('[a-z]{2}_[A-Z]{2}\\.UTF-8',locale):raise RuntimeError('請選擇有效的 UTF-8 系統語言。')
    apt(['locales','tzdata']+locale_packages(locale))
    supported=Path('/usr/share/i18n/SUPPORTED').read_text()
    if locale+' UTF-8' not in supported:raise RuntimeError('Ubuntu 不支援此 Locale。')
    log('設定時區：'+timezone);run(['timedatectl','set-timezone',timezone])
    log('生成語言環境：'+locale);stream(['locale-gen',locale]);run(['update-locale','LANG='+locale])
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
    if count!=1:raise RuntimeError('無法安全識別 xrdp 監聽設定，未修改配置。')
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
    if not active('xrdp'):raise RuntimeError('xrdp 啟動失敗，請檢查系統日誌。')
    log('XFCE 與 xrdp 已就緒。RDP 僅監聽迴環地址，透過 SSH 連線。')

def setup_chrome(options):
    account()
    if os.uname().machine!='x86_64':raise RuntimeError('Google Chrome 當前僅支援本安裝器的 x86_64 Ubuntu。')
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
    # 從當前桌面環境啟動，保留 Chrome 沙箱，不使用 --no-sandbox。
    user_write('Desktop/Chrome.desktop','[Desktop Entry]\nType=Application\nName=Chrome\nExec=google-chrome-stable\nIcon=google-chrome\nTerminal=false\n',0o755)
    log(version('/usr/bin/google-chrome-stable'))

def setup_input(options):
    account();apt(['fcitx5','fcitx5-pinyin','fcitx5-frontend-gtk3','fcitx5-frontend-qt5','fonts-noto-cjk'])
    setup_xsession()
    user_write('.config/fcitx5/profile','[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=pinyin\n\n[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n[Groups/0/Items/1]\nName=pinyin\nLayout=\n\n[GroupOrder]\n0=Default\n')
    user_write('.config/autostart/fcitx5.desktop','[Desktop Entry]\nType=Application\nName=Fcitx5\nExec=fcitx5 -d\nTerminal=false\n')
    log('拼音與中文字體已就緒，重新登入桌面後生效。')

def file_sha256(file):
    digest=hashlib.sha256()
    for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def download_claude(target):
    import platform,socket,urllib.request,urllib.error
    target_platform={'x86_64':'linux-x64','aarch64':'linux-arm64'}.get(platform.machine())
    if not target_platform:raise RuntimeError('Claude Code 當前僅支援 x86_64 或 ARM64 Ubuntu。')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            return None

    opener=urllib.request.build_opener(NoRedirect())
    base='https://downloads.claude.ai/claude-code-releases'
    def fetch(path):return opener.open(base+path,timeout=45)
    try:
        log('正在透過 VPS 網絡獲取 Claude Code 官方版本和校驗資訊…')
        with fetch('/stable') as response:release=response.read(129).decode('ascii',errors='replace').strip()
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',release):raise RuntimeError('Claude Code 官方版本資訊無效。')
        with fetch('/'+release+'/manifest.json') as response:manifest_bytes=response.read(1024*1024+1)
        try:
            if len(manifest_bytes)>1024*1024:raise ValueError('manifest size')
            digest=json.loads(manifest_bytes)['platforms'][target_platform]['checksum']
        except (ValueError,KeyError,TypeError) as error:
            raise RuntimeError('Claude Code 官方校驗資訊無效。') from error
        if not isinstance(digest,str) or not re.fullmatch('[a-fA-F0-9]{64}',digest):raise RuntimeError('Claude Code 官方校驗資訊無效。')
        received=0;progress=time.monotonic();started=progress
        with fetch('/'+release+'/'+target_platform+'/claude') as response, target.open('xb') as output:
            target.chmod(0o600)
            while True:
                block=response.read(1024*1024)
                if not block:break
                received+=len(block)
                if received>1024**3:raise RuntimeError('Claude Code 下載檔案異常過大，已停止安裝。')
                if time.monotonic()-started>600:raise TimeoutError('下載超時')
                output.write(block)
                if time.monotonic()-progress>=10:
                    log('VPS 正在下載 Claude Code：已接收 '+str(received//1024//1024)+' MB…');progress=time.monotonic()
        with target.open('rb') as source:
            if file_sha256(source)!=digest.lower():raise RuntimeError('Claude Code 下載檔案校驗失敗，未執行安裝。')
        log('VPS 下載完成，官方 Claude Code 檔案校驗通過。')
        return release
    except Exception as error:
        target.unlink(missing_ok=True)
        if isinstance(error,(TimeoutError,socket.timeout)):raise OperationError('timeout','VPS 下載 Claude Code 官方檔案超時。') from error
        if isinstance(error,urllib.error.URLError):
            dns=isinstance(error.reason,socket.gaierror)
            raise OperationError('dns_failure' if dns else 'package_fetch_failed','VPS 無法連接 Claude Code 官方下載服務'+('：DNS 域名解析失敗。' if dns else '。')) from error
        raise

def setup_claude(options):
    account();apt(['curl','ca-certificates','git','tmux'])
    with tempfile.TemporaryDirectory(prefix='claude-install-',dir='/opt/safe-code') as directory:
        source=Path(directory)/'claude'
        release=download_claude(source)
        # The verified file can be read, but cannot be replaced by the work user.
        source.chmod(0o444);Path(directory).chmod(0o755)
        log('正在為普通工作用戶安裝已校驗的 Claude Code…')
        user_files('install_claude','.local/bin/claude',source=str(source),release=release)
    value=version(home()/'.local/bin/claude')
    if not value:raise RuntimeError('Claude Code 安裝後未找到有效程式。')
    user_files('append_unique','.profile',content='export PATH="$HOME/.local/bin:$PATH"',mode=0o644)
    log(value+'；請由本人完成 Claude 帳號授權。')

def setup_ssh(options):
    account();public=options.get('publicKey','').strip()
    if not re.fullmatch(r'(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+ safe-code-[A-Za-z0-9_-]+',public):raise RuntimeError('客戶端 SSH 公鑰格式無效。')
    user_files('append_unique','.ssh/authorized_keys',content=public,mode=0o600)
    user_write('.local/bin/safe-code-terminal','#!/bin/sh\ncd "$HOME/workspace"\nexec tmux new-session -A -s safe-code\n',0o755)
    log('工作用戶公鑰已授權，原 SSH 配置及管理入口保持。')

def install_mount(options):apt(['davfs2','fuse3']);log('目錄掛載組件已安裝。')

def setup_git_identity():
    a=account();entry=pwd.getpwnam(a['username'])
    if entry.pw_uid==0:raise RuntimeError('Git 身份必須配置在普通工作用戶下。')
    if not shutil.which('git'):raise RuntimeError('尚未安裝 Git，請先安裝 Claude Code 或重新安裝 CloudCLI。')
    command=['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','LANG=C.UTF-8','git','config','--global']
    identity={}
    for key,default in [('user.name',a['username']),('user.email',a['username']+'@safe-code.invalid')]:
        existing=run(command+['--get',key],check=False)
        if existing.returncode not in (0,1):raise RuntimeError('無法讀取工作用戶的 Git 配置，請檢查檔案權限。')
        value=existing.stdout.strip()
        if not value:
            run(command+[key,default]);value=default
            log('已補全本地 Git 預設身份，可在 CloudCLI 設定中修改。')
        identity[key]=value
    return identity

def setup_cloud_onboarding(options):
    identity=setup_git_identity()
    report=json.loads(user_files('cloud_onboarding','.local/state/safe-code-cloud/auth.db',gitName=identity['user.name'],gitEmail=identity['user.email']).stdout)
    log('CloudCLI 首次設定已完成，保留現有帳號與 Git 身份；可直接進入主介面。')
    sync_cloud_mount_projects()
    return report

def setup_cloud(options):
    secure_root_directory('/mnt/safe-code',0o755)
    a=account();apt(['curl','xz-utils','ca-certificates','git'])
    setup_git_identity()
    # Node 獨立放置，避免覆蓋用戶已有執行階段。
    node_root=Path('/opt/safe-code/node')
    if not (node_root/'bin/node').exists():
        index=json.loads(run(['curl','-fsSL','https://nodejs.org/dist/index.json']).stdout)
        release=next(x for x in index if x['version'].startswith('v22.') and x.get('lts'))
        v=release['version'];name='node-'+v+'-linux-x64.tar.xz';url='https://nodejs.org/dist/'+v+'/'
        sums=run(['curl','-fsSL',url+'SHASUMS256.txt']).stdout
        expected=next(line.split()[0] for line in sums.splitlines() if line.split()[-1]==name)
        archive=Path('/opt/safe-code')/name
        stream(['curl','-fL','--retry','3',url+name,'-o',str(archive)])
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise RuntimeError('Node 下載校驗失敗。')
        run(['tar','-xJf',str(archive),'-C','/opt/safe-code']);os.rename('/opt/safe-code/'+name[:-7],node_root)
    app=Path('/opt/safe-code/cloudcli');app.mkdir(exist_ok=True)
    env={**os.environ,'PATH':str(node_root/'bin')+':'+os.environ['PATH'],'SHARP_IGNORE_GLOBAL_LIBVIPS':'1'}
    stream([str(node_root/'bin/npm'),'install','--prefix',str(app),'--omit=optional','--no-audit','--no-fund','@cloudcli-ai/cloudcli@1.37.7'],env)
    # 補全終端機搜尋所需的平台程式，仍使用官方 npm registry。
    stream([str(node_root/'bin/npm'),'install','--prefix',str(app),'--no-audit','--no-fund','@vscode/ripgrep-linux-x64'],env)
    cli=app/'node_modules/@cloudcli-ai/cloudcli/dist-server/server/index.js'
    if not cli.exists():raise RuntimeError('CloudCLI 缺少服務入口，未建立服務。')
    user_write('.local/state/safe-code-cloud/.keep','')
    unit='''[Unit]\nDescription=Safe Code CloudCLI\nAfter=network-online.target\n[Service]\nType=simple\nUser={user}\nWorkingDirectory={home}\nEnvironment=HOME={home}\nEnvironment=HOST=127.0.0.1\nEnvironment=SERVER_PORT=3001\nEnvironment=DATABASE_PATH={home}/.local/state/safe-code-cloud/auth.db\nEnvironment=NODE_ENV=production\nEnvironment=NODE_OPTIONS=--max-old-space-size=384\nEnvironment=CLAUDE_CLI_PATH={home}/.local/bin/claude\nEnvironment=PATH={node}/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin\nExecStart={node}/bin/node {cli}\nRestart=on-failure\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=read-only\nReadWritePaths={home} /mnt/safe-code\nMemoryMax=1G\n[Install]\nWantedBy=multi-user.target\n'''.format(user=a['username'],home=home(),node=node_root,cli=cli)
    path=Path('/etc/systemd/system/safe-code-cloudcli.service');changed=not path.exists() or path.read_text()!=unit
    write(path,unit)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','safe-code-cloudcli'])
    if changed:run(['systemctl','restart','safe-code-cloudcli'])
    time.sleep(3)
    if not active('safe-code-cloudcli'):raise RuntimeError('CloudCLI 啟動失敗，請檢查專屬服務日誌。')
    setup_cloud_onboarding(options)
    log('CloudCLI 已就緒。首次建立網頁帳戶後可直接進入主介面，Claude 帳號授權由本人完成。')

def setup_cloud_public(options):
    require_ubuntu()
    service_path=Path('/etc/systemd/system/safe-code-cloudcli.service')
    if not service_path.exists() or not active('safe-code-cloudcli'):
        log('正在準備 CloudCLI 基礎服務…')
        setup_cloud(options)
    log('正在配置 CloudCLI 允許公網訪問 (0.0.0.0:3001)…')
    content=service_path.read_text()
    if 'Environment=HOST=127.0.0.1' in content:
        content=content.replace('Environment=HOST=127.0.0.1','Environment=HOST=0.0.0.0')
        write(service_path,content)
        run(['systemctl','daemon-reload'])
        log('正在重新啟動 CloudCLI 服務以應用配置…')
        run(['systemctl','restart','safe-code-cloudcli'])
        time.sleep(3)
    elif not active('safe-code-cloudcli'):
        run(['systemctl','restart','safe-code-cloudcli'])
        time.sleep(3)
    if not active('safe-code-cloudcli'):
        raise RuntimeError('CloudCLI 啟動失敗，請檢查專屬服務日誌。')
    log('正在檢查防火牆配置…')
    if shutil.which('ufw'):
        try:
            status=run(['ufw','status'],False).stdout
            if 'Status: active' in status:
                run(['ufw','allow','3001/tcp'],False)
                log('已在 UFW 防火牆放行 TCP 3001 連接埠。')
        except Exception:
            pass
    log('正在獲取公網訪問地址…')
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
    log(f'CloudCLI 已成功開放公網訪問：{url}')
    log('提示：請確保網頁密碼安全。')
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
        if not (app/'dist-server/server/modules/database/repositories/projects.db.js').is_file():raise RuntimeError('CloudCLI 版本暫不支援項目聯動，請更新後重新開啟。')
        index=app/'dist/index.html'
        if index.is_symlink() or not index.is_file():raise RuntimeError('CloudCLI 網頁入口異常，未修改網頁。')
        report=json.loads(user_files('cloud_projects','.local/state/safe-code-cloud/auth.db',projects=cloud_project_targets()).stdout)
        if not report['ready']:return None
        script=app/'dist/safe-code-mounts.js';revision=app/'dist/safe-code-mount-revision.json'
        # These assets contain no paths, account data or authentication tokens.
        write(script,CLOUD_MOUNT_REFRESH)
        html=index.read_text();tag='<script src="/safe-code-mounts.js" data-safe-code-mounts></script>'
        if 'data-safe-code-mounts' not in html:
            if '<head>' not in html:raise RuntimeError('CloudCLI 網頁入口不相容，未修改網頁。')
            backup=index.with_name('index.before-safe-code-mounts.html')
            if not backup.exists():write(backup,html)
            write(index,html.replace('<head>','<head>\n    '+tag,1))
        if report['changed'] or not revision.exists():write(revision,json.dumps({'revision':secrets.token_hex(16)}))
        log('CloudCLI 已同步掛載項目；網頁項目列表會自動重新整理。')
        return None
    except Exception as error:
        warning='目錄掛載保持正常，CloudCLI 項目聯動未完成：'+str(error)
        log(warning);return warning

NODE_ROOT=Path('/opt/safe-code/node')

def supports_gateway_node(node):
    return bool(node and run([node, '-p', 'Number(process.versions.node.split(".")[0]) >= 20'],
                             check=False, timeout=5).stdout.strip()=='true')

def gateway_node():
    node=NODE_ROOT/'bin/node'
    if node.is_file():
        if not supports_gateway_node(node):
            raise RuntimeError('Safe Code API 需要 Node.js 20 或以上，請更新 VPS 上現有的 Node.js。')
        return node
    if NODE_ROOT.exists() or NODE_ROOT.is_symlink():
        raise RuntimeError('Safe Code 的 Node.js 安裝不完整，請檢查 VPS 上的 Node.js 後重試。')
    system_node=shutil.which('node')
    if supports_gateway_node(system_node):
        return Path(system_node)
    arch={'x86_64':'x64','aarch64':'arm64'}.get(os.uname().machine)
    if not arch:
        raise RuntimeError('當前伺服器架構不支援自動安裝 Node.js。')
    if not shutil.which('curl'):
        raise RuntimeError('VPS 缺少 curl。請透過 Safe Code 部署 Claude Code，然後重試。')
    log('正在從 Node.js 官方渠道安裝所需執行階段…')
    source='https://nodejs.org/dist/'
    curl=['curl','-q','-fLSs','--proto','=https','--connect-timeout','8']
    releases=json.loads(run([*curl,'--max-time','14',source+'index.json'],timeout=16).stdout)
    release=next((item['version'] for item in releases
                  if item.get('lts') and re.fullmatch(r'v22\.[0-9]+\.[0-9]+',item.get('version',''))), None)
    if release is None:
        raise RuntimeError('未能取得官方 Node.js 長期支援版本，請稍後重試。')
    filename=f'node-{release}-linux-{arch}.tar.gz'
    url=source+release+'/'
    sums=run([*curl,'--max-time','14',url+'SHASUMS256.txt'],timeout=16).stdout
    digest=next((line.split()[0] for line in sums.splitlines()
                 if re.fullmatch(r'[a-f0-9]{64}\s+'+re.escape(filename),line)), None)
    if digest is None:
        raise RuntimeError('官方 Node.js 下載檔案缺少 SHA-256 校驗資訊。')
    with tempfile.TemporaryDirectory(prefix='node-install-',dir=NODE_ROOT.parent) as folder:
        stage=Path(folder)
        archive=stage/filename
        run([*curl,'--max-time','75',url+filename,'-o',archive],timeout=77)
        with archive.open('rb') as source_file:
            if file_sha256(source_file)!=digest:
                raise RuntimeError('Node.js 下載檔案校驗失敗，未安裝。')
        run(['tar','-xzf',archive,'--no-same-owner','-C',stage],timeout=24)
        candidate=stage/filename.removesuffix('.tar.gz')
        if not supports_gateway_node(candidate/'bin/node'):
            raise RuntimeError('官方 Node.js 執行階段未通過版本檢查。')
        candidate.rename(NODE_ROOT)
    return node

CODEX_BRIDGE_SOURCE_HASH='3d6c8d98604e7301bd92046b00ead3b08eddefa157e1c5661d2c9599aefe58c3'
def codex_bridge(options):
    action=options.get('action','');a=account();parameters={k:options[k] for k in ['publicKey','keyId','clientId']}
    if action=='install':
        import zipfile
        payload=Path(options.get('payload',''))
        if payload.parent!=Path(__file__).resolve().parent or payload.is_symlink() or not re.fullmatch(r'codex-bridge-[a-f0-9]{32}\.zip',payload.name):raise RuntimeError('適配器原始碼包路徑無效。')
        data=payload.read_bytes()
        if hashlib.sha256(data).hexdigest()!=CODEX_BRIDGE_SOURCE_HASH:raise RuntimeError('適配器原始碼包校驗失敗，未安裝。')
        node=Path('/opt/safe-code/node/bin/node')
        if not node.exists():
            candidate=shutil.which('node')
            if candidate and int(run([candidate,'-p','process.versions.node.split(".")[0]']).stdout.strip())>=20:node=Path(candidate)
            else:
                apt(['curl','xz-utils','ca-certificates'])
                index=json.loads(run(['curl','-fsSL','https://nodejs.org/dist/index.json']).stdout);release=next(x for x in index if x['version'].startswith('v22.') and x.get('lts'))
                version=release['version'];arch={'x86_64':'x64','aarch64':'arm64'}.get(os.uname().machine)
                if not arch or not re.fullmatch(r'v22\.[0-9]+\.[0-9]+',version):raise RuntimeError('當前架構或 Node.js 版本不支援適配器自動安裝。')
                name='node-'+version+'-linux-'+arch+'.tar.xz';url='https://nodejs.org/dist/'+version+'/'
                expected=next(line.split()[0] for line in run(['curl','-fsSL',url+'SHASUMS256.txt']).stdout.splitlines() if line.split()[-1]==name)
                archive=Path('/opt/safe-code')/name;stream(['curl','-fL','--retry','3',url+name,'-o',str(archive)])
                if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise RuntimeError('Node.js 檔案校驗失敗。')
                with tempfile.TemporaryDirectory(prefix='node-bridge-',dir='/opt/safe-code') as stage:
                    run(['tar','-xJf',str(archive),'-C',stage]);candidate=Path(stage)/name[:-7]
                    run([str(candidate/'bin/node'),'--version']);os.rename(candidate,node.parent.parent)
        if int(run([str(node),'-p','process.versions.node.split(".")[0]']).stdout.strip())<20:raise RuntimeError('適配器需要 Node.js 20 或更高版本。')
        claude=home()/'.local/bin/claude'
        if not claude.exists():raise RuntimeError('請先為工作用戶部署 Claude Code，並自行完成 Claude 登入。')
        with zipfile.ZipFile(payload) as archive:
            files={name:base64.b64encode(archive.read(name)).decode() for name in archive.namelist()}
        parameters.update({'files':files,'node':str(node),'claude':str(claude),'sourceHash':CODEX_BRIDGE_SOURCE_HASH,'username':a['username']})
    return json.loads(user_files('codex_bridge','.local/state/safe-code/codex-bridge',action=action,**parameters).stdout)

API_DIRECTORY=Path('/opt/safe-code/desktop-api')
API_UNIT=Path('/etc/systemd/system/safe-code-desktop-api.service')
CLOUD_UNIT=Path('/etc/systemd/system/safe-code-cloudcli.service')
API_FILES={'install.sh','desktop_gateway.py','server.py','management.py','claude_usage.py',
           'pricing.py','configure_cloudcli.py','host_control_mcp.py','device_bridge.py','device_service.py','prompt_review.py','codex_bridge.zip'}
API_STATE='.local/state/safe-code-desktop-api'

def safe_code_api(options):
    action=options.get('action')
    if action not in ('list','usage','issue','update','unbind','revoke','delete','settings','set_port','set_origin','set_concurrency','public_status','enable_public','refresh_subscription','install',
                      'ticket_endpoint','key_host_reuse','bind_ssh_device','model_catalog','set_prompt_review','set_key_queue','repair'):
        raise RuntimeError('未知 Safe Code API 管理操作。')
    entry=pwd.getpwnam(account()['username'])
    if entry.pw_uid==0:raise RuntimeError('工作帳戶不能是 root。')
    script=API_DIRECTORY/'server.py'
    directory=Path(entry.pw_dir)/API_STATE
    work_python=['runuser','-u',entry.pw_name,'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin',
                 'LANG=C.UTF-8','/usr/bin/python3']
    command=[*work_python,'-B',str(script),'--state',str(directory)]
    if action=='install':
        return install_safe_code_api(entry, command, options, work_python)
    if action=='repair':
        return repair_safe_code_api(entry, command, options, work_python)
    if not script.is_file():
        if action=='public_status':
            return {'installed':False,'enabled':False,
                    'cloudcliConfigured':(Path(entry.pw_dir)/'.config/safe-code/cloudcli-gateway.json').is_file()}
        raise RuntimeError('當前 VPS 尚未安裝 Safe Code API 管理服務。')
    if action == 'public_status':
        status = safe_code_api_admin(command, options)
        return {**status, 'cloudcliConfigured':(Path(entry.pw_dir)/'.config/safe-code/cloudcli-gateway.json').is_file()}
    if action == 'model_catalog':
        return safe_code_api_admin(command, options, timeout=27)
    if action in ('enable_public', 'issue', 'key_host_reuse', 'bind_ssh_device'):
        status = safe_code_api_admin(command, {'action':'public_status'})
        if status.get('backend') != 'cloudcli':
            raise RuntimeError('當前 VPS 需要先配置 CloudCLI 並升級 API 服務。請開啟 Safe Code 的公網網關完成配置。')
    if action == 'set_port':
        return change_safe_code_api_port(command, options)
    if action == 'refresh_subscription':
        status = safe_code_api_admin(command, {'action':'public_status'})
        if not status.get('enabled'):
            raise ValueError('請先啟用 Safe Code API 公網服務。')
        source = Path(__file__).with_name('claude_usage.py').read_text()
        output = run([*work_python, '-I', '-'], stdin=source, timeout=18)
        sample = json.loads(output.stdout)
        if status.get('gatewayRevision', 0) >= 19 and sample.get('state') == 'available':
            return safe_code_api_admin(command, {'action':'remember_subscription', 'sample':sample})
        return sample
    if action in ('key_host_reuse','bind_ssh_device'):
        settings = safe_code_api_admin(command, {'action':'settings'})
        # CloudCLI's gateway requires a loopback bind and uses its own device routing.
        if ipaddress.ip_address(settings['bind']).is_loopback:
            return {'allowed': False}
        reply = user_files('gateway_key_host_reuse', API_STATE, keyId=options.get('keyId'), pin=options.get('pin'))
        allowed = json.loads(reply.stdout)
        if action == 'key_host_reuse':
            return allowed
        if not allowed.get('allowed'):
            raise ValueError('此 Key 無法綁定當前 VPS 的 SSH 控制通道。')
        binding = run([*command, 'bind-ssh-device'],stdin=json.dumps({
            'keyId':options.get('keyId'), 'hostClientId':options.get('hostClientId'), 'hostname':options.get('hostname'),
        }),timeout=8)
        value = json.loads(binding.stdout)
        if value.get('error'):
            raise ValueError(value['error'])
        return value['value']
    if action in ('issue', 'ticket_endpoint'):
        origin = gateway_public_origin(safe_code_api_admin(command, {'action':'settings'}), options.get('hints'))
        if action == 'ticket_endpoint':
            certificate = json.loads(user_files('gateway_origin', API_STATE, keyId=options.get('keyId')).stdout)
            return {'origin': origin, 'pin': certificate['pin']}
        options = {key: value for key, value in options.items() if key != 'hints'}
        options['origin'] = origin
    return safe_code_api_admin(command, options)

def gateway_public_origin(settings, hints):
    if settings.get('origin'):
        return settings['origin']
    bind = ipaddress.ip_address(settings['bind'])
    if bind.is_loopback:
        raise ValueError('此 VPS 網關僅監聽迴環地址，請在網關設定中填寫可從公網訪問的 HTTPS 地址。')
    port = settings['port']
    hints = hints if isinstance(hints, dict) else {}
    resolved = hints.get('resolved')
    assigned = hints.get('assignedIps')
    addresses = [settings.get('publicIp'), str(bind), hints.get('sshHost')]
    if isinstance(resolved, list):
        addresses.extend(resolved[:8])
    if isinstance(assigned, list):
        addresses.extend(assigned[:16])
    addresses.append(hints.get('egressIp'))
    for candidate in addresses:
        if not isinstance(candidate, str) or '%' in candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate.removeprefix('[').removesuffix(']'))
        except ValueError:
            continue
        if address.version == bind.version and address.is_global and not address.is_multicast:
            host = str(address) if address.version == 4 else f'[{address}]'
            return f'https://{host}:{port}'
    raise ValueError('未識別到 VPS 的公網 IP。請用 VPS 控制台顯示的公網 IP 重新建立 SSH 連線後再建立密鑰。')

def gateway_install_files(options, folder):
    files=options.get('files')
    if not isinstance(files,dict) or set(files)!=API_FILES or any(
        not isinstance(value,str) or len(value)>2*1024*1024 for value in files.values()
    ):
        raise ValueError('Safe Code API 安裝組件不完整，請重新安裝客戶端。')
    for name,data in files.items():
        (folder/name).write_bytes(base64.b64decode(data,validate=True))
    if hashlib.sha256((folder/'codex_bridge.zip').read_bytes()).hexdigest()!=CODEX_BRIDGE_SOURCE_HASH:
        raise ValueError('Safe Code API 安裝組件校驗失敗。')

def managed_service(unit, directory, user, start_arguments):
    # Refuse another installer, distro unit, or drop-in that redirects execution elsewhere.
    for name,expected in [('FragmentPath',str(unit)),('User',user),('WorkingDirectory',str(directory))]:
        if run(['systemctl','show','-p',name,'--value',unit.name],timeout=8).stdout.strip()!=expected:
            raise RuntimeError('無法確認 '+unit.name+' 由 Safe Code 管理，未更改服務。')
    exec_start=run(['systemctl','show','-p','ExecStart','--value',unit.name],timeout=8).stdout
    if 'argv[]='+start_arguments+' ;' not in exec_start or unit.is_symlink() or not unit.is_file() or unit.stat().st_uid!=0 or unit.stat().st_mode&0o022:
        raise RuntimeError('檢測到非標準 '+unit.name+' 啟動配置，未更改服務。')

def installed_gateway_revision(path):
    if path.is_symlink() or not path.is_file():
        return None
    with path.open('rb') as stream:
        match=re.search(rb'^GATEWAY_REVISION\s*=\s*(\d+)\s*$',stream.read(2*1024*1024),re.M)
    return int(match[1]) if match else None

def stage_cloudcli_repair(cloud, staged):
    node_root=API_DIRECTORY.parent/'node'
    npm=node_root/'bin/npm'
    for name in ('package.json','package-lock.json'):
        source=cloud/name
        if source.is_symlink() or not source.is_file() or source.stat().st_uid!=0 or source.stat().st_mode&0o022:
            raise RuntimeError('未找到 Safe Code 安裝的 CloudCLI 鎖定清單。')
        shutil.copyfile(source,staged/name)
    locked=json.loads((staged/'package-lock.json').read_text())['packages']
    package='node_modules/@cloudcli-ai/cloudcli'
    version=locked.get(package,{}).get('version','')
    if not re.fullmatch(r'\d+\.\d+\.\d+',version) or not npm.is_file():
        raise RuntimeError('無法核對已安裝的 CloudCLI 官方版本或專用 Node。')
    for name,record in locked.items():
        if name and (record.get('link') or not record.get('resolved','').startswith('https://registry.npmjs.org/')
                     or not record.get('integrity')):
            raise RuntimeError('CloudCLI 鎖定清單含非官方源或缺少完整性摘要。')
    log('正在準備 CloudCLI 官方原版 '+version+'…')
    env={'PATH':str(npm.parent)+':/usr/sbin:/usr/bin:/bin','HOME':str(staged.parent),'LANG':'C.UTF-8',
         'SHARP_IGNORE_GLOBAL_LIBVIPS':'1'}
    if (npm.parent.parent/'include/node/node.h').is_file():
        env['npm_config_nodedir']=str(npm.parent.parent)
    global_config=staged.parent/'global.npmrc'
    global_config.touch(mode=0o600)
    install=[str(npm),'ci','--prefix',str(staged),'--userconfig','/dev/null','--globalconfig',str(global_config),
             '--no-audit','--no-fund','--prefer-offline']
    try:
        run(install,env=env,timeout=360)
    except RuntimeError as error:
        lacks_toolchain=any(not shutil.which(tool,path=env['PATH']) for tool in ('make','g++','python3'))
        if 'gyp ERR!' not in str(error) or not lacks_toolchain:
            raise
        log('CloudCLI 的原生依賴需要在本機編譯，正在安裝 Ubuntu 構建工具…')
        apt(['build-essential','python3'])
        run(install,env=env,timeout=360)
    current=json.loads((staged/package/'package.json').read_text())
    if current.get('name')!='@cloudcli-ai/cloudcli' or current.get('version')!=version:
        raise RuntimeError('CloudCLI 官方軟件包版本與鎖定清單不一致。')

def repair_safe_code_api(entry, command, options, work_python):
    if options.get('accepted') is not True or type(options.get('reinstallCloudCLI',False)) is not bool:
        raise ValueError('請先確認故障修復範圍。')
    with_cloud=options.get('reinstallCloudCLI',False)
    cloud=API_DIRECTORY.parent/'cloudcli'
    cloud_unit=CLOUD_UNIT
    cloud_script='node_modules/@cloudcli-ai/cloudcli/dist-server/server/index.js'
    state=Path(entry.pw_dir)/API_STATE
    if state.resolve().is_relative_to(API_DIRECTORY.resolve()):
        raise RuntimeError('網關數據位於程式目錄，無法安全重新安裝。')
    secure_root_directory(STATE,0o700)
    secure_root_directory(API_DIRECTORY.parent,0o755)
    secure_root_directory(API_UNIT.parent,0o755)
    with (STATE/'install.lock').open('a') as install_lock, (STATE/'desktop-api-port.lock').open('a') as port_lock:
        try:
            for lock in (install_lock,port_lock):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationError('deployment_busy','VPS 有部署或網關設定正在處理，請稍後再試。') from None
        managed_service(API_UNIT,API_DIRECTORY,entry.pw_name,'/usr/bin/python3 -B '+str(API_DIRECTORY/'server.py')+' --state '+str(state)+' serve')
        if API_DIRECTORY.exists() or API_DIRECTORY.is_symlink():
            secure_root_directory(API_DIRECTORY,0o755)
        if with_cloud:
            secure_root_directory(cloud,0o755)
            managed_service(cloud_unit,Path(entry.pw_dir),entry.pw_name,
                            str(cloud.parent/'node/bin/node')+' '+str(cloud/cloud_script))
        with tempfile.TemporaryDirectory(prefix='desktop-api-repair-input-',dir=STATE) as folder:
            source=Path(folder)
            gateway_install_files(options,source)
            revision=installed_gateway_revision(source/'server.py')
            previous=installed_gateway_revision(API_DIRECTORY/'server.py')
            if revision is None or previous is not None and previous>revision:
                raise ValueError('VPS 網關版本比客戶端內置版本新，或安裝組件版本未知。請先更新 Safe Code 客戶端。')
            return replace_gateway_programs(entry,command,source,state,work_python,revision,
                                            (cloud,cloud_unit,cloud_script) if with_cloud else None)

def provision_gateway_devices(entry, state):
    run(['/usr/bin/python3','-B',str(API_DIRECTORY/'device_service.py'),
         entry.pw_name,str(API_DIRECTORY),str(state)],timeout=250)

def replace_gateway_programs(entry, command, source, state, work_python, revision, cloud_install):
    log('正在準備當前客戶端內置的網關安裝組件…')
    program=Path(tempfile.mkdtemp(prefix='.desktop-api-repair-',dir=API_DIRECTORY.parent))
    replacements=[(API_DIRECTORY,program)]
    units=[API_UNIT]
    keep_backups=False
    swapped=[]
    stopped=[]
    previously_active={}
    try:
        service=Path(tempfile.mkdtemp(prefix='.desktop-api-repair-',dir=API_UNIT.parent))
        replacements.append((API_UNIT,service))
        run(['/bin/bash',str(source/'install.sh'),'--prepare-repair',entry.pw_name,str(API_DIRECTORY),
             str(state),str(program/'new'),str(service/'new')],timeout=45)
        for script in (program/'new').glob('*.py'):
            compile(script.read_bytes(),script.name,'exec')
        if cloud_install:
            cloud,cloud_unit,cloud_script=cloud_install
            package=Path(tempfile.mkdtemp(prefix='.cloudcli-repair-',dir=cloud.parent))
            replacements.append((cloud,package))
            units.append(cloud_unit)
            raw_env=run(['systemctl','show','-p','Environment','--value',cloud_unit.name],timeout=8).stdout
            cloud_env=dict(item.split('=',1) for item in shlex.split(raw_env) if '=' in item)
            env_files=run(['systemctl','show','-p','EnvironmentFiles','--value',cloud_unit.name],timeout=8).stdout
            database=Path(cloud_env.get('DATABASE_PATH',''))
            cloud_home=Path(cloud_env.get('HOME',entry.pw_dir))
            if env_files.strip() or any(not path.is_absolute() or path.resolve().is_relative_to(cloud.resolve())
                                        for path in (database,cloud_home)):
                raise RuntimeError('無法確認 CloudCLI 的帳號和對話數據位於程式目錄外，未更改服務。')
            port=cloud_env.get('SERVER_PORT','3001')
            if not port.isdecimal() or not 1<=int(port)<=65535:
                raise ValueError('CloudCLI 的伺服器端口無效。')
            (package/'new').mkdir(mode=0o755)
            stage_cloudcli_repair(cloud,package/'new')
            if not (package/'new'/cloud_script).is_file():
                raise RuntimeError('CloudCLI 安裝包缺少服務入口。')
        previously_active={unit:active(unit.name) for unit in units}
        try:
            settings=safe_code_api_admin(command,{'action':'settings'},timeout=3)
        except (RuntimeError,subprocess.TimeoutExpired):
            settings={}  # A broken admin socket is one reason to run repair.
        if settings.get('busy'):
            raise RuntimeError('網關還有任務正在處理，請等任務完成後再修復。')
        log('正在停止舊服務並更換程式檔案，原有網關數據保留…')
        for unit in units:
            run(['systemctl','stop',unit.name],timeout=30)
            stopped.append(unit)
        for target,backup in replacements:
            if target.exists():
                target.rename(backup/'previous')
            swapped.append((target,backup))
            (backup/'new').rename(target)
        run(['systemctl','daemon-reload'],timeout=15)
        provision_gateway_devices(entry,state)
        for unit in reversed(units):
            run(['systemctl','start',unit.name],timeout=30)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                if cloud_install:
                    run([*work_python,'-B',str(API_DIRECTORY/'configure_cloudcli.py'),'--check-cloudcli',port],timeout=5)
                status=safe_code_api_admin(command,{'action':'public_status'},timeout=3)
                if status.get('backend')=='cloudcli' and status.get('gatewayRevision')==revision:
                    log('網關重新安裝完成，服務檢查通過。')
                    return {**status,'cloudcliReinstalled':bool(cloud_install)}
            except (RuntimeError,subprocess.TimeoutExpired):
                pass
            time.sleep(.5)
        raise RuntimeError('重新安裝後的網關或 CloudCLI 未通過就緒檢查。')
    except Exception as error:
        if not stopped:
            raise
        try:
            if swapped:
                for unit in stopped:
                    run(['systemctl','stop',unit.name],timeout=30)
                for target,backup in reversed(swapped):
                    if target.exists():target.rename(backup/'failed')
                    if (backup/'previous').exists():(backup/'previous').rename(target)
                run(['systemctl','daemon-reload'],timeout=15)
            for unit in reversed(stopped):
                if previously_active[unit]:
                    run(['systemctl','start',unit.name],timeout=30)
        except Exception:
            keep_backups=True
            raise RuntimeError('網關重新安裝失敗，自動回滾未完成。VPS 上的原程式備份目錄 '+', '.join(
                str(backup) for _,backup in replacements)+'。') from error
        raise RuntimeError(str(error)+' 已恢復原有程式檔案和服務設定。') from error
    finally:
        if not keep_backups:
            for _,backup in replacements:
                shutil.rmtree(backup)

def install_safe_code_api(entry, command, options, work_python):
    if options.get('accepted') is not True:
        raise ValueError('請先確認安裝 Safe Code API 服務。')
    secure_root_directory(STATE,0o700)
    with (STATE/'install.lock').open('a') as guard:
        try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise OperationError('deployment_busy','VPS 有部署正在執行，請等待完成後重試。') from None
        status={'action':'public_status'}
        upgrading = (API_DIRECTORY/'server.py').is_file()
        if not upgrading and (API_DIRECTORY.exists() or API_DIRECTORY.is_symlink() or API_UNIT.exists() or API_UNIT.is_symlink()
            or run(['systemctl','show','-p','LoadState','--value',API_UNIT.name],check=False).stdout.strip()!='not-found'):
            raise RuntimeError('檢測到已有的 Safe Code API 檔案或服務。請在 VPS 修復現有安裝後重試。')
        secure_root_directory(API_DIRECTORY.parent,0o755)
        work_home=Path(entry.pw_dir)
        if upgrading:
            existing = safe_code_api_admin(command, status)
            if existing.get('enabled'):
                settings = safe_code_api_admin(command, {'action':'settings'})
                if settings.get('busy'):
                    raise RuntimeError('網關還有請求正在處理，請等網關空閒後再升級。')
            if (API_DIRECTORY.is_symlink()
                or run(['systemctl','show','-p','User','--value',API_UNIT.name]).stdout.strip()!=entry.pw_name):
                raise RuntimeError('無法確認現有 Safe Code API 服務歸屬，請檢查 VPS 服務配置。')
        with tempfile.TemporaryDirectory(prefix='desktop-api-install-',dir=STATE) as folder:
            gateway_install_files(options,Path(folder))
            # Stage code under a root-owned directory and send credentials only over stdin.
            credentials = options.get('cloudcli') or {}
            if not isinstance(credentials,dict):
                raise ValueError('CloudCLI API Key 格式無效。')
            key = credentials.get('apiKey')
            if key is not None and not isinstance(key,str):
                raise ValueError('CloudCLI API Key 格式無效。')
            credentials = {'apiKey': key} if key else {}
            if key:
                REDACTIONS.append(key)
            with tempfile.TemporaryDirectory(prefix='safe-code-cloudcli-') as preflight:
                path = Path(preflight)
                path.chmod(0o755)
                for name in ('configure_cloudcli.py','desktop_gateway.py','host_control_mcp.py'):
                    shutil.copyfile(Path(folder)/name, path/name)
                    (path/name).chmod(0o644)
                configure = [*work_python,'-B',str(path/'configure_cloudcli.py')]
                config_path = work_home/'.config/safe-code/cloudcli-gateway.json'
                if not config_path.is_file():
                    probe = run([*configure,'--check-cloudcli'],check=False,timeout=8)
                    if probe.returncode:
                        if active('safe-code-cloudcli'):
                            raise RuntimeError('CloudCLI 已執行，但本機授權接口不可用，請檢查服務。')
                        if Path('/etc/systemd/system/safe-code-cloudcli.service').is_file():
                            run(['systemctl','start','safe-code-cloudcli'],timeout=30)
                        else:
                            log('正在安裝 CloudCLI…')
                            setup_cloud({})
                        for _ in range(6):
                            probe = run([*configure,'--check-cloudcli'],check=False,timeout=8)
                            if not probe.returncode:
                                break
                            time.sleep(1)
                        if probe.returncode:
                            raise RuntimeError('CloudCLI 已啟動，但本機授權接口尚未就緒。')
                    pending = config_path.with_name('cloudcli-gateway-onboarding.json')
                    if (not credentials and (json.loads(probe.stdout)['needsSetup'] or pending.is_file())
                        and run([*work_python,'-I','-c','import websocket'],check=False,timeout=5).returncode):
                        apt(['python3-websocket'])
                checked = json.loads(run([*configure,'--prepare-public-gateway',str(work_home/API_STATE)],
                    stdin=json.dumps(credentials),timeout=40).stdout)
                if checked.get('error'):
                    raise ValueError(checked['error'])
                if checked.get('cloudcliKeyRequired'):
                    return {'cloudcliKeyRequired':True}
                bind = checked['bind']
                created = checked.get('created')
                generated = checked.get('cloudcliAccount')
                def rollback_config():
                    if created:
                        run([*configure,'--rollback-public-gateway',created],timeout=8)
                def installed(state):
                    if generated:
                        REDACTIONS.append(generated['password'])
                        try:
                            run([*configure,'--finish-public-gateway'],check=False,timeout=8)
                        except (OSError,subprocess.TimeoutExpired):
                            pass
                        state['cloudcliAccount'] = generated
                    return state
                if upgrading:
                    if existing.get('enabled') and safe_code_api_admin(command, {'action':'settings'}).get('busy'):
                        rollback_config()
                        raise RuntimeError('網關還有請求正在處理，請等網關空閒後再升級。')
                    return installed(upgrade_safe_code_api(command, Path(folder), rollback_config, entry))
                try:
                    run(['/bin/bash',str(Path(folder)/'install.sh'),entry.pw_name,bind,'45143'],
                        env={**os.environ,'SAFECODE_INITIAL_API_INSTALL':'1'},timeout=250)
                    deadline=time.monotonic()+8
                    while time.monotonic()<deadline:
                        try:
                            state = safe_code_api_admin(command,status,timeout=2)
                            if state.get('backend')=='cloudcli':return installed(state)
                        except (RuntimeError,subprocess.TimeoutExpired):
                            pass
                        time.sleep(.25)
                    raise RuntimeError('CloudCLI 網關未能啟動，請檢查 VPS 上的服務日誌。')
                except (RuntimeError,OSError,subprocess.TimeoutExpired):
                    if API_UNIT.is_file():
                        try:run(['systemctl','disable','--now',API_UNIT.name],timeout=12)
                        except (RuntimeError,OSError,subprocess.TimeoutExpired) as error:
                            raise RuntimeError('Safe Code API 安裝失敗，VPS 服務未能回退，請檢查該服務的狀態。') from error
                        API_UNIT.unlink()
                        run(['systemctl','daemon-reload'],check=False,timeout=8)
                    if API_DIRECTORY.is_dir():shutil.rmtree(API_DIRECTORY)
                    rollback_config()
                    raise

def upgrade_safe_code_api(command, source, rollback_config, entry):
    names = sorted(name for name in API_FILES if name.endswith('.py'))
    original = {}
    for name in names:
        target = API_DIRECTORY/name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            rollback_config()
            raise ValueError('檢測到非標準網關組件，未覆蓋現有服務。')
        original[name] = target.read_bytes() if target.exists() else None
    try:
        for name in names:
            write_api_component(API_DIRECTORY/name, (source/name).read_bytes())
        try:
            provision_gateway_devices(entry,Path(entry.pw_dir)/API_STATE)
        except (OSError,RuntimeError,subprocess.TimeoutExpired):
            log('私有裝置服務尚未就緒，普通 API 可繼續使用。請在網關設定中執行故障修復。')
        run(['systemctl','restart',API_UNIT.name],timeout=30)
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            try:
                state = safe_code_api_admin(command,{'action':'public_status'},timeout=2)
                if state.get('backend')=='cloudcli':
                    return state
            except (RuntimeError,subprocess.TimeoutExpired):
                pass
            time.sleep(.25)
        raise RuntimeError('更新後的 CloudCLI 網關未能通過啟動檢查。')
    except (OSError,RuntimeError,subprocess.TimeoutExpired):
        for name,data in original.items():
            target=API_DIRECTORY/name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                write_api_component(target,data)
        rollback_config()
        run(['systemctl','restart',API_UNIT.name],timeout=30)
        raise RuntimeError('CloudCLI 網關更新失敗，已恢復之前的網關組件。') from None

def write_api_component(target, data):
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:
        staged=Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(),0o644)
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)

def safe_code_api_admin(command, options, timeout=12):
    process = run([*command, 'admin'], check=False, stdin=json.dumps(options), timeout=timeout)
    try:
        value = json.loads(process.stdout)
        if process.returncode or not isinstance(value, dict):
            raise ValueError()
    except ValueError:
        raise RuntimeError('當前 VPS 的 Safe Code API 管理服務尚未更新或無法訪問。')
    if value.get('error'):
        raise RuntimeError(value['error'])
    return value['value']

def change_safe_code_api_port(command, options):
    port = options.get('port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('監聽連接埠必須是 1024 到 65535 之間的整數。')
    secure_root_directory(STATE, 0o700)
    with (STATE/'desktop-api-port.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('正在修改網關監聽連接埠，請稍後重試。') from None
        current = safe_code_api_admin(command, {'action': 'settings'})
        previous = current['port']
        if previous != options.get('expectedPort'):
            raise RuntimeError('監聽連接埠已變更，請重新讀取網關狀態。')
        if port == previous:
            return current
        if current['busy']:
            raise RuntimeError('網關還有正在執行或排隊的請求，請等網關空閒後再儲存。')
        run([*command, 'configure-port', str(previous), str(port)], timeout=10)
        try:
            return restart_safe_code_api(command, port)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            try:
                run([*command, 'configure-port', str(port), str(previous), '--rollback'], timeout=10)
                restart_safe_code_api(command, previous)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                raise RuntimeError('新連接埠啟動失敗，原連接埠也未能恢復，請檢查 VPS 上的 Safe Code API 服務。') from None
            raise RuntimeError('新連接埠啟動失敗，已恢復原監聽連接埠。') from None

def restart_safe_code_api(command, port):
    run(['systemctl', 'restart', 'safe-code-desktop-api.service'], timeout=30)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            current = safe_code_api_admin(command, {'action': 'settings'}, timeout=2)
            if current['port'] == port:
                return current
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.2)
    raise RuntimeError('Safe Code API 未在預期連接埠啟動。')

def host_bridge(options):
    client=options.get('clientId','');action=options.get('action','')
    if not re.fullmatch('[a-f0-9]{20}',client):raise RuntimeError('本機控制標識無效。')
    if action in ('configure','verify'):
        port=options.get('port')
        if not isinstance(port,int) or not 1024<=port<=65535:raise RuntimeError('本機控制連接埠無效。')
        listeners=run(['ss','-H','-lnt','sport = :'+str(port)]).stdout
        if '127.0.0.1:'+str(port) not in listeners or any(x in listeners for x in ['0.0.0.0:'+str(port),'*:'+str(port),'[::]:'+str(port)]):raise RuntimeError('本機控制必須只綁定伺服器迴環地址。')
    return json.loads(user_files('host_bridge','.ssh/safe-code-host/'+client+'/identity',**options).stdout)

def mount_client(options):
    client=options.get('clientId','')
    if not re.fullmatch('[a-f0-9]{20}',client):raise OperationError('invalid_argument','掛載客戶端標識無效。')
    return client

def remove_mounts(options):
    client=mount_client(options);record=STATE/('mount-'+client+'.json')
    if not record.exists():return {'clientId':client,'paths':[]}
    previous=json.loads(record.read_text())
    for target in reversed(previous.get('paths',[])):
        if os.path.ismount(target):
            log('同步並卸載目錄：'+target)
            completed=subprocess.run(['umount',target],capture_output=True,text=True,timeout=90)
            if completed.returncode:raise RuntimeError('目錄仍在使用，未關閉檔案通道：'+target+'；'+completed.stderr[-400:])
    for alias,destination in zip(previous.get('aliases',[]),previous.get('paths',[])):
        try:user_files('remove_alias',str(Path(alias).relative_to(home())),destination=destination)
        except Exception:log('用戶已修改目錄入口，保留該入口，不執行管理員刪除。')
    secrets_file=SECRETS_PATH
    if secrets_file.exists():
        lines=secrets_file.read_text().splitlines()
        urls=set(previous.get('urls',[]))
        lines=[line for line in lines if not any(line.startswith(url+' ') for url in urls) and line!='# safe-code-'+client]
        write(secrets_file,'\n'.join(lines)+'\n',0o600)
    record.unlink();warning=sync_cloud_mount_projects() if options.get('syncProjects',True) else None
    return {'clientId':client,'paths':[],'cloudWarning':warning}

def mounted_paths():
    return {line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()}

def mount_secret_lines(client,urls):
    if not SECRETS_PATH.exists():return []
    return [line for line in SECRETS_PATH.read_text().splitlines() if line=='# safe-code-'+client or any(line.startswith(url+' ') for url in urls)]

def write_mount_secrets(client,urls,user='',password='',extra=None):
    lines=SECRETS_PATH.read_text().splitlines() if SECRETS_PATH.exists() else []
    urls=set(urls)
    lines=[line for line in lines if line!='# safe-code-'+client and not any(line.startswith(url+' ') for url in urls)]
    if extra is None:
        lines.append('# safe-code-'+client)
        lines.extend(url+' '+user+' '+password for url in urls)
    else:lines.extend(extra)
    write(SECRETS_PATH,'\n'.join(lines)+'\n',0o600)

def mount_davfs(target,url,config,uid,gid):
    try:completed=subprocess.run(['mount','-t','davfs',url,str(target),'-o','conf='+str(config)+',uid='+str(uid)+',gid='+str(gid)+',file_mode=0600,dir_mode=0700,noexec,nosuid,nodev'],input='',text=True,capture_output=True,timeout=75)
    except subprocess.TimeoutExpired:raise OperationError('mount_timeout','建立目錄掛載超時，請檢查 SSH 檔案通道後重試。')
    if completed.returncode:raise OperationError('mount_failed','目錄掛載失敗：'+(completed.stderr or completed.stdout or '')[-800:])
    if not os.path.ismount(str(target)):raise OperationError('mount_failed','目標未成為實際掛載點。')

def link_mount_alias(alias,target):
    try:user_files('alias',str(Path(alias).relative_to(home())),destination=str(target))
    except Exception as error:raise OperationError('mount_target_invalid','無法建立目錄入口：'+str(error)) from error

def restore_mounts(client,previous,secret_lines):
    """新配置失敗後恢復原記錄和憑據；剩餘缺失項交給守護進程 mount_recover。"""
    if not previous or not previous.get('aliases') or not secret_lines:return False
    aliases=previous.get('aliases',[]);urls=previous.get('urls',[])
    if len(aliases)!=len(urls):return False
    record=STATE/('mount-'+client+'.json')
    write(record,json.dumps({'paths':[],'urls':urls,'aliases':aliases}),0o600)
    write_mount_secrets(client,urls,extra=secret_lines)
    config=STATE/('mount-'+client)/'davfs2.conf'
    if not config.exists():return False
    entry=pwd.getpwnam(account()['username']);uid=entry.pw_uid;gid=entry.pw_gid
    targets=previous.get('paths',[])
    if len(targets)>len(aliases):return False
    mounted=[]
    for target,url,alias in zip(targets,urls,aliases):
        target=Path(target);secure_root_directory(target,0o755)
        log('恢復原目錄掛載：'+str(target));mount_davfs(str(target),url,config,uid,gid)
        mounted.append(str(target));write(record,json.dumps({'paths':mounted,'urls':urls,'aliases':aliases}),0o600)
        link_mount_alias(alias,str(target))
    return bool(mounted)

def mount_project_paths(client,aliases,previous_targets):
    managed=Path('/mnt/safe-code')/account()['username']/client
    registry=STATE/('mount-project-paths-'+client+'.json')
    saved=json.loads(registry.read_text()) if registry.exists() else {}
    targets=[previous_targets.get(alias,saved.get(alias,str(managed/('p'+hashlib.sha256(alias.encode()).hexdigest()[:20])))) for alias in aliases]
    if len(set(targets))!=len(targets) or any(Path(path).parent!=managed or not re.fullmatch(r'm[0-9]+|p[a-f0-9]{20}',Path(path).name) for path in targets):raise OperationError('mount_target_invalid','掛載項目路徑記錄異常，已保留現有掛載。')
    return targets,registry,{**saved,**dict(zip(aliases,targets))}

def apply_mounts(options,recover=False):
    a=account();client=mount_client(options)
    if not package_installed('davfs2'):raise OperationError('operation_failed','請先安裝目錄掛載組件。')
    port=options.get('port');user=options.get('username');password=options.get('password','');mappings=options.get('mappings',[])
    if not isinstance(port,int) or not 1024<=port<=65535 or user!='safecode' or not re.fullmatch('[a-f0-9]{48}',password):raise OperationError('invalid_argument','檔案通道認證參數無效。')
    if not 1<=len(mappings)<=12:raise OperationError('invalid_argument','請選擇 1–12 組目錄。')
    REDACTIONS.append(password)
    listeners=run(['ss','-H','-lnt','sport = :'+str(port)]).stdout
    if '127.0.0.1:'+str(port) not in listeners or any(x in listeners for x in ['0.0.0.0:'+str(port),'*:'+str(port),'[::]:'+str(port)]):raise OperationError('mount_failed','反向檔案連接埠未限定為迴環地址，請檢查 sshd 的 GatewayPorts 設定。')
    aliases=[]
    previous_record=STATE/('mount-'+client+'.json')
    previous=json.loads(previous_record.read_text()) if previous_record.exists() else {}
    previous_targets=dict(zip(previous.get('aliases',[]),previous.get('paths',[])))
    previous_secret_lines=mount_secret_lines(client,previous.get('urls',[])) if not recover else []
    log('正在檢查伺服器目錄…')
    for mapping in mappings:
        value=mapping.get('remote','');target=Path(value)
        if not target.is_absolute() or '..' in target.parts or not target.parent.resolve().is_relative_to(home()):raise OperationError('mount_target_invalid','掛載目標必須位於工作用戶的主目錄下。')
        if target==home():raise OperationError('mount_target_invalid','不能覆蓋整個用戶目錄。')
        if str(target) in aliases:raise OperationError('mount_target_invalid','掛載目標不能重複。')
        if any(target.is_relative_to(Path(alias)) or Path(alias).is_relative_to(target) for alias in aliases):raise OperationError('mount_target_invalid','掛載目錄不能相互包含，請選擇獨立的目錄。')
        try:user_files('check_alias',str(target.relative_to(home())),destination=previous_targets.get(str(target),''))
        except Exception as error:raise OperationError('mount_target_invalid',str(error)) from error
        aliases.append(str(target))
    targets,path_registry,remembered_paths=mount_project_paths(client,aliases,previous_targets)
    if recover and (not previous or previous.get('aliases')!=aliases or not set(previous.get('paths',[])).issubset(set(targets))):raise OperationError('mount_target_invalid','原掛載記錄不一致，已保留現有掛載，請檢查後重新儲存。')
    log('開始應用目錄掛載配置…')
    existing_mounts=mounted_paths()
    # 先完成不會破壞舊掛載的目錄、配置和 FUSE 檢查，再卸載舊掛載。
    try:
        managed=Path('/mnt/safe-code');secure_root_directory(managed,0o755);managed=managed/a['username'];secure_root_directory(managed,0o755);managed=managed/client;secure_root_directory(managed,0o755)
        for target in targets:
            if target not in existing_mounts:secure_root_directory(target,0o755)
        directory=STATE/('mount-'+client);directory.mkdir(mode=0o700,exist_ok=True)
        config=directory/'davfs2.conf'
        write(config,'use_locks 0\ndelay_upload 0\ncache_size 1024\nconnect_timeout 10\nread_timeout 120\nretry 5\nmax_retry 30\nmax_upload_attempts 1000000\n',0o600)
        if not Path('/dev/fuse').exists():run(['modprobe','fuse'])
    except OperationError:raise
    except Exception as error:raise OperationError('access_denied','建立目錄掛載環境失敗：'+str(error)) from error
    uid=pwd.getpwnam(a['username']).pw_uid;gid=pwd.getpwnam(a['username']).pw_gid
    urls=['http://127.0.0.1:'+str(port)+'/m'+str(i)+'/' for i in range(len(targets))]
    if recover and previous.get('urls')!=urls:raise OperationError('mount_target_invalid','原掛載通道已變化，未卸載現有目錄，請人工檢查。')
    old_removed=False
    if not recover:
        try:remove_mounts({'clientId':client,'syncProjects':False})
        except Exception as error:raise OperationError('mount_failed','未能卸載原有目錄：'+str(error)) from error
        old_removed=True
    record=STATE/('mount-'+client+'.json')
    mounted=[x for x in targets if x in existing_mounts] if recover else []
    try:
        if not recover:
            write_mount_secrets(client,urls,user,password)
            write(record,json.dumps({'paths':[],'urls':urls,'aliases':aliases}),0o600)
        for target,url,alias in zip(targets,urls,aliases):
            if target in mounted:continue
            target=Path(target)
            log('建立加密檔案通道：'+str(target));mount_davfs(str(target),url,config,uid,gid)
            mounted.append(str(target));write(record,json.dumps({'paths':[x for x in targets if x in mounted],'urls':urls,'aliases':aliases}),0o600)
            link_mount_alias(alias,str(target))
        # Keep legacy m0/m1 paths after unmount too, so upgrading retains IDs.
        write(path_registry,json.dumps(remembered_paths),0o600)
        warning=sync_cloud_mount_projects()
        return {'clientId':client,'paths':aliases,'managedPaths':mounted,'cloudWarning':warning}
    except Exception as error:
        cleanup_error=None
        if not recover:
            try:remove_mounts({'clientId':client,'syncProjects':False})
            except Exception as cleanup:cleanup_error=cleanup
        if old_removed and cleanup_error is None:
            try:
                restored=restore_mounts(client,previous,previous_secret_lines)
                log('新掛載未完成，已恢復原目錄掛載。' if restored else '新掛載未完成，已保留原目錄記錄，等待掛載守護恢復。')
            except Exception as restore_error:log('恢復原目錄掛載未完成：'+str(restore_error))
        if cleanup_error is not None:
            code=error.code if isinstance(error,OperationError) else 'mount_failed'
            raise OperationError(code,str(error)+'\n清理未完成：'+str(cleanup_error)) from error
        raise

def start_job(request):
    operation=request.get('op')
    if operation not in LONG_OPERATIONS:raise RuntimeError('此操作不能作為背景安裝任務。')
    job=secrets.token_hex(16);directory=STATE/'jobs'/job;directory.mkdir(mode=0o700,parents=True)
    request={**request,'background':False,'jobId':job}
    write(directory/'request.json',json.dumps(request),0o600)
    write(directory/'metadata.json',json.dumps({'jobId':job,'operation':operation,'created':time.time()}),0o600)
    for name in ['agent.py','network.py','socks_egress.py']:
        shutil.copy2(Path(__file__).parent/name,directory/name);(directory/name).chmod(0o600)
    write(directory/'output.log','',0o600)
    run(['systemd-run','--quiet','--collect','--unit=safe-code-job-'+job,'--property=Type=exec','--property=StandardOutput=append:'+str(directory/'output.log'),'--property=StandardError=append:'+str(directory/'output.log'),'/usr/bin/python3','-u',str(directory/'agent.py'),str(directory/'request.json')])
    return {'jobId':job,'operation':operation}

def job_directory(job):
    if not re.fullmatch('[a-f0-9]{32}',job):raise RuntimeError('背景任務標識無效。')
    directory=STATE/'jobs'/job
    if not directory.is_dir():raise RuntimeError('背景任務記錄不存在。')
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
        if status not in ['active','activating'] and age>3:error='遠端任務意外結束，重連後可檢查實際安裝狀態。'
    return {'lines':lines,'nextOffset':next_offset,'done':value is not None or error is not None,'value':value,'error':error,'errorCode':failure.get('code')}

def main():
    global JOB_DIR
    require_ubuntu() # 必須先檢查系統，再建立任何目錄或寫入配置。
    if sys.argv[1]=='--list-directories':
        if os.geteuid()!=0:raise RuntimeError('需要安裝管理員權限。')
        result(list_directories(json.loads(base64.b64decode(sys.argv[2]))));return
    if sys.argv[1]=='--poll':
        if os.geteuid()!=0:raise RuntimeError('需要安裝管理員權限。')
        result(poll_job(sys.argv[2],sys.argv[3]));return
    request_path=Path(sys.argv[1])
    request=json.loads(request_path.read_text())
    if (request.get('op') == 'safe_code_api' and (request.get('options') or {}).get('cloudcli')
        and request_path.parent == Path(__file__).parent and re.fullmatch(r'request-[0-9a-f]{32}\.json', request_path.name)):
        request_path.unlink(missing_ok=True)
    if request.get('jobId'):JOB_DIR=job_directory(request['jobId'])
    op=request.get('op','inspect')
    if op in ('socks_probe','socks_enable'):
        Path(sys.argv[1]).unlink(missing_ok=True)
    if op=='inspect':result(inspect());return
    if op=='egress_probe':
        value=run(['curl','--noproxy','*','-4','-fsS','--connect-timeout','5','--max-time','12','https://api.ipify.org']).stdout.strip()
        result({'ip':str(ipaddress.IPv4Address(value))});return
    if os.geteuid()!=0:raise RuntimeError('安裝需要 root 或免密 sudo 權限。')
    if op=='socks_status':
        import socks_egress
        result(socks_egress.status());return
    if op=='socks_probe':
        import socks_egress
        if socks_egress.load_state():raise RuntimeError('請先停用已配置的 SOCKS5 出口。')
        result(socks_egress.probe(socks_egress.parse_proxy((request.get('options') or {}).get('proxyUrl'))));return
    if op=='account_info':result(account());return
    if op=='list_directories':result(list_directories(request.get('options') or {}));return
    if op=='create_directory':result(create_directory(request.get('options') or {}));return
    if op=='safe_code_api_repair':
        options=request.get('options') or {}
        if options.get('action')!='repair' or options.get('accepted') is not True:
            raise ValueError('請先確認故障修復範圍。')
        secure_root_directory(STATE,0o700)
        result(start_job(request) if request.get('background') else safe_code_api(options));return
    if op=='safe_code_api':result(safe_code_api(request.get('options') or {}));return
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
        except BlockingIOError:raise OperationError('deployment_busy','另一個部署正在執行，請等待完成。')
        options=request.get('options') or {}
        if options.get('password'):REDACTIONS.append(options['password'])
        if op in ('socks_enable','socks_disable'):
            import network,socks_egress
            with network.transaction_lock():
                if op=='socks_enable':
                    proxy=socks_egress.parse_proxy(options.get('proxyUrl'))
                    if not shutil.which('nft'):apt(['nftables'])
                    result(socks_egress.enable(proxy));return
                result(socks_egress.disable());return
        if op.startswith('ip_'):
            import network
            if op=='ip_prepare':
                if (STATE/'socks-egress.json').exists():raise RuntimeError('請先停用開發模式的 SOCKS5 出口，再更換已綁定的 IP。')
                apt(['nftables','curl']);result(network.prepare(options));return
            if op=='ip_apply':result(network.schedule_apply(options['token']));return
            if op=='ip_commit':result(network.commit(options['token']));return
            if op=='ip_rollback':result(network.rollback(options['token']));return
            raise RuntimeError('未知網絡操作。')
        if op=='codex_bridge':result(codex_bridge(options));return
        if op=='host_bridge':result(host_bridge(options));return
        if op=='mount_apply':result(apply_mounts(options));return
        if op=='mount_recover':result(apply_mounts(options,recover=True));return
        if op=='mount_remove':result(remove_mounts(options));return
        mapping={'account':setup_account,'localization':setup_locale,'desktop':setup_desktop,'chrome':setup_chrome,'input':setup_input,'claude':setup_claude,'ssh':setup_ssh,'mount_install':install_mount,'cloud':setup_cloud,'cloud_public':setup_cloud_public,'cloud_setup':setup_cloud_onboarding}
        if op not in mapping:raise RuntimeError('未知部署操作。')
        log('開始部署：'+op)
        if (op=='account' and not read_config()) or op in ['localization','desktop','chrome','input','claude','cloud','mount_install']:
            prepare_system()
        mapping[op](options)
        result(inspect())

if __name__=='__main__':
    try:main()
    except Exception as error:
        message=str(error)
        for secret in REDACTIONS:
            if secret:message=message.replace(secret,'[已隱藏]')
        if JOB_DIR is not None:write(JOB_DIR/'error.json',json.dumps({'error':message,'code':error_code(error)},ensure_ascii=False),0o600)
        print('SC_ERROR_CODE:'+error_code(error),flush=True)
        print('SC_ERROR_JSON:'+json.dumps(message,ensure_ascii=False),flush=True);sys.exit(1)
