#!/usr/bin/env python3
"""Safe Code Ubuntu 部署端。仅由客户端显式请求运行；不包含服务凭据。"""
import contextlib,fcntl,hashlib,ipaddress,json,os,pwd,re,secrets,shlex,shutil,subprocess,sys,time,tempfile
from pathlib import Path
STATE=Path('/var/lib/safe-code')
REDACTIONS=[]
JOB_DIR=None
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

def run(args,check=True,env=None,stdin=None):
    p=subprocess.run(args,text=True,capture_output=True,input=stdin,env=env,timeout=180)
    if check and p.returncode:raise RuntimeError((p.stderr or p.stdout or '命令执行失败')[-1600:])
    return p

def stream(args,env=None):
    p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,bufsize=1)
    for line in p.stdout:
        if line.strip():log(line.rstrip())
    if p.wait():raise RuntimeError('安装命令退出，状态码 '+str(p.returncode))

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
checked=target.parent.resolve() if operation in ['alias','remove_alias'] else target.resolve()
if not checked.is_relative_to(base):raise ValueError('用户配置路径指向主目录之外，已拒绝操作。')
os.umask(0o077)
if operation=='read':
    if not target.is_file():print(json.dumps(''));sys.exit(0)
    with target.open() as f:value=f.read(1048577)
    if len(value)>1048576:raise ValueError('用户配置文件过大。')
    print(json.dumps(value));sys.exit(0)
if operation=='alias':
    destination=pathlib.Path(request['destination'])
    if target.is_symlink():
        if target.readlink()!=destination:raise ValueError('目标已存在其他符号链接，未覆盖。')
        sys.exit(0)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):raise ValueError('挂载目标不是空目录，未覆盖。')
        target.rmdir()
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
    return run(['runuser','-u',a['username'],'--','env','-i','HOME='+entry.pw_dir,'PATH=/usr/bin:/bin','LANG=C.UTF-8','python3','-I','-c',USER_FILES],stdin=json.dumps(request))

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

def apt(packages):
    missing=[p for p in packages if not package_installed(p)]
    if not missing:log('所需组件已安装，跳过重复下载。');return
    env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'}
    opts=['apt-get','-o','DPkg::Lock::Timeout=180','-o','Acquire::Retries=3']
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
        localized=timezone==desired.get('timezone') and Path('/etc/default/locale').exists() and desired.get('locale','') in Path('/etc/default/locale').read_text()
    pub=user_read('.ssh/authorized_keys') if account_ok else ''
    statuses={
      'account':account_ok,
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
        table=run(['nft','list','table','inet','safe_code_ip'],False).stdout
        healthy=('Safe Code managed' in table and (not policy.get('lockEgress') or 'ip saddr != '+policy['egressIp']+' drop' in table) and (not policy.get('lockIngress') or 'ip daddr != '+policy['ingressIp']+' tcp dport '+str(policy.get('sshPort',22))+' drop' in table))
    language=Path('/etc/default/locale').read_text() if Path('/etc/default/locale').exists() else ''
    match=re.search(r'(?m)^LANG="?([^\s"]+)',language)
    return {'osId':info.get('ID',''),'osName':info.get('PRETTY_NAME',''),'osVersion':info.get('VERSION_ID',''),
      'architecture':os.uname().machine,'memoryBytes':mem,'diskFreeBytes':disk.free,'cpuCount':os.cpu_count(),
      'homeDirectory':str(h), 'isRoot':os.geteuid()==0,'username':a['username'] if account_ok else '', 'statuses':statuses,
      'assignedIps':addresses,'timezone':run(['timedatectl','show','-p','Timezone','--value'],False).stdout.strip(),
      'systemLocale':match[1] if match else '', 'ipPolicyHealthy':healthy,
      'claudeVersion':version(h/'.local/bin/claude'),'desktopReady':statuses['desktop'],
      'cloudReady':active('safe-code-cloudcli'),'mountReady':package_installed('davfs2'),
      'ipPolicy':policy,'activeJobs':active_jobs() if os.geteuid()==0 else []}

def setup_account(options):
    existing=read_config()
    if existing:
        if run(['id',existing['username']],False).returncode:raise RuntimeError('工作用户记录与系统不一致，请手动检查。')
        log('复用已有工作账户，不更改现有密码。');return
    user=options.get('username','workspace')
    if not re.fullmatch('[a-z][a-z0-9_-]{1,30}',user):raise RuntimeError('用户名须为 2–31 位小写字母、数字、下划线或短横线。')
    if run(['id',user],False).returncode==0:raise RuntimeError('此用户已存在，拒绝覆盖其密码。请选择新工作用户名。')
    password=secrets.token_urlsafe(20)
    run(['useradd','-m','-s','/bin/bash',user])
    run(['chpasswd'],stdin=user+':'+password+'\n')
    write(STATE/'account.json',json.dumps({'username':user,'password':password}),0o600)
    user_write('workspace/.keep','')
    log('工作账户与项目目录已创建。')

def setup_locale(options):
    account()
    timezone=options.get('timezone','');locale=options.get('locale','')
    if not timezone or '..' in timezone or not (Path('/usr/share/zoneinfo')/timezone).is_file():raise RuntimeError('请选择有效的 IANA 时区。')
    if not re.fullmatch('[a-z]{2}_[A-Z]{2}\\.UTF-8',locale):raise RuntimeError('请选择有效的 UTF-8 系统语言。')
    apt(['locales','tzdata'])
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

def setup_claude(options):
    a=account();apt(['curl','ca-certificates','git','tmux'])
    target=Path('/opt/safe-code/install-claude.sh')
    script=run(['curl','-fsSL','--retry','3','https://claude.ai/install.sh']).stdout
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

def setup_cloud(options):
    secure_root_directory('/mnt/safe-code',0o755)
    a=account();apt(['curl','xz-utils','ca-certificates'])
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
    log('CloudCLI 已通过回环端口启动。请经 SSH 隧道打开并创建网页账户。')

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
    record.unlink();return {'clientId':client,'paths':[]}

def apply_mounts(options):
    a=account();client=mount_client(options)
    if not package_installed('davfs2'):raise RuntimeError('请先安装目录挂载组件。')
    port=options.get('port');user=options.get('username');password=options.get('password','');mappings=options.get('mappings',[])
    if not isinstance(port,int) or not 1024<=port<=65535 or user!='safecode' or not re.fullmatch('[a-f0-9]{48}',password):raise RuntimeError('文件通道认证参数无效。')
    if not 1<=len(mappings)<=12:raise RuntimeError('请选择 1–12 组目录。')
    REDACTIONS.append(password)
    listeners=run(['ss','-H','-lnt','sport = :'+str(port)]).stdout
    if '127.0.0.1:'+str(port) not in listeners or any(x in listeners for x in ['0.0.0.0:'+str(port),'*:'+str(port),'[::]:'+str(port)]):raise RuntimeError('反向文件端口未限定为回环地址，请检查 sshd 的 GatewayPorts 设置。')
    aliases=[]
    for mapping in mappings:
        value=mapping.get('remote','');target=Path(value)
        if not target.is_absolute() or '..' in target.parts or not target.parent.resolve().is_relative_to(home()):raise RuntimeError('挂载目标必须位于工作用户的主目录下。')
        if target==home():raise RuntimeError('不能覆盖整个用户目录。')
        if str(target) in aliases:raise RuntimeError('挂载目标不能重复。')
        aliases.append(str(target))
    remove_mounts({'clientId':client})
    managed=Path('/mnt/safe-code');secure_root_directory(managed,0o755);managed=managed/a['username'];secure_root_directory(managed,0o755);managed=managed/client;secure_root_directory(managed,0o755)
    targets=[str(managed/('m'+str(i))) for i in range(len(aliases))]
    for target in targets:secure_root_directory(target,0o755)
    if not Path('/dev/fuse').exists():run(['modprobe','fuse'])
    directory=STATE/('mount-'+client);directory.mkdir(mode=0o700,exist_ok=True)
    config=directory/'davfs2.conf'
    write(config,'use_locks 0\ndelay_upload 0\ncache_size 20\n',0o600)
    uid=pwd.getpwnam(a['username']).pw_uid;gid=pwd.getpwnam(a['username']).pw_gid
    urls=['http://127.0.0.1:'+str(port)+'/m'+str(i)+'/' for i in range(len(targets))]
    path=Path('/etc/davfs2/secrets');old=path.read_text() if path.exists() else ''
    write(path,old.rstrip()+'\n# safe-code-'+client+'\n'+'\n'.join(url+' '+user+' '+password for url in urls)+'\n',0o600)
    record=STATE/('mount-'+client+'.json');write(record,json.dumps({'paths':[],'urls':urls,'aliases':aliases}),0o600)
    mounted=[]
    try:
        for target,url,alias in zip(targets,urls,aliases):
            target=Path(target)
            log('建立加密文件通道：'+str(target))
            completed=subprocess.run(['mount','-t','davfs',url,str(target),'-o','conf='+str(config)+',uid='+str(uid)+',gid='+str(gid)+',file_mode=0600,dir_mode=0700,noexec,nosuid,nodev'],input='',text=True,capture_output=True,timeout=75)
            if completed.returncode:raise RuntimeError('目录挂载失败：'+completed.stderr[-800:])
            if not os.path.ismount(target):raise RuntimeError('目标未成为实际挂载点。')
            mounted.append(str(target));write(record,json.dumps({'paths':mounted,'urls':urls,'aliases':aliases}),0o600)
            user_files('alias',str(Path(alias).relative_to(home())),destination=str(target))
        return {'clientId':client,'paths':aliases,'managedPaths':mounted}
    except Exception:
        remove_mounts({'clientId':client});raise

def start_job(request):
    operation=request.get('op')
    if operation not in LONG_OPERATIONS:raise RuntimeError('此操作不能作为后台安装任务。')
    job=secrets.token_hex(16);directory=STATE/'jobs'/job;directory.mkdir(mode=0o700,parents=True)
    request={**request,'background':False,'jobId':job}
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
            lines=[x for x in data.decode('utf-8',errors='replace').splitlines() if not x.startswith(('SC_RESULT:','SC_ERROR:'))]
    value=json.loads((directory/'result.json').read_text()) if (directory/'result.json').exists() else None
    error=json.loads((directory/'error.json').read_text()).get('error') if (directory/'error.json').exists() else None
    if value is None and error is None:
        status=run(['systemctl','is-active','safe-code-job-'+job+'.service'],False).stdout.strip()
        age=time.time()-json.loads((directory/'metadata.json').read_text())['created']
        if status not in ['active','activating'] and age>3:error='远端任务意外结束，重连后可检查实际安装状态。'
    return {'lines':lines,'nextOffset':next_offset,'done':value is not None or error is not None,'value':value,'error':error}

def main():
    global JOB_DIR
    require_ubuntu() # 必须先检查系统，再创建任何目录或写入配置。
    if sys.argv[1]=='--poll':
        if os.geteuid()!=0:raise RuntimeError('需要安装管理员权限。')
        result(poll_job(sys.argv[2],sys.argv[3]));return
    request=json.loads(Path(sys.argv[1]).read_text())
    if request.get('jobId'):JOB_DIR=job_directory(request['jobId'])
    op=request.get('op','inspect')
    if op=='inspect':result(inspect());return
    if op=='egress_probe':
        value=run(['curl','-4','-fsS','--connect-timeout','5','--max-time','12','https://api.ipify.org']).stdout.strip()
        result({'ip':str(ipaddress.IPv4Address(value))});return
    if os.geteuid()!=0:raise RuntimeError('安装需要 root 或免密 sudo 权限。')
    if op=='account_info':result(account());return
    secure_root_directory(STATE,0o700)
    write('/etc/needrestart/conf.d/99-safe-code.conf', '$nrconf{override_rc}->{qr(^(?:ssh|xrdp(?:-sesman)?)(?:\\.service)?$)} = 0;\n')
    secure_root_directory('/opt/safe-code',0o755)
    if request.get('background'):
        result(start_job(request));return
    with (STATE/'install.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('另一个部署正在运行，请等待完成。')
        options=request.get('options') or {}
        if options.get('password'):REDACTIONS.append(options['password'])
        if op.startswith('ip_'):
            import network
            if op=='ip_prepare':
                apt(['nftables','curl']);result(network.prepare(options));return
            if op=='ip_apply':result(network.apply(options['token']));return
            if op=='ip_commit':result(network.commit(options['token']));return
            if op=='ip_rollback':result(network.rollback(options['token']));return
            raise RuntimeError('未知网络操作。')
        if op=='mount_apply':result(apply_mounts(options));return
        if op=='mount_remove':result(remove_mounts(options));return
        mapping={'account':setup_account,'localization':setup_locale,'desktop':setup_desktop,'chrome':setup_chrome,'input':setup_input,'claude':setup_claude,'ssh':setup_ssh,'mount_install':install_mount,'cloud':setup_cloud}
        if op not in mapping:raise RuntimeError('未知部署操作。')
        log('开始部署：'+op)
        mapping[op](options)
        result(inspect())

if __name__=='__main__':
    try:main()
    except Exception as error:
        message=str(error)
        for secret in REDACTIONS:
            if secret:message=message.replace(secret,'[已隐藏]')
        if JOB_DIR is not None:write(JOB_DIR/'error.json',json.dumps({'error':message},ensure_ascii=False),0o600)
        print('SC_ERROR:'+message,flush=True);sys.exit(1)
