"""Provision the gateway's private FRP service and its authorization callback."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

from device_bridge import PORTS, device_service_problem, write_file

VERSION = '0.71.1'
ARCHIVES = {
    'x86_64': ('amd64', '1d9875210972a5d1a62b2dc4c48df45ab0af05d3ca1b5e494a014428259df803'),
    'aarch64': ('arm64', '8edf590b0c396fa42f9c147121a76af83cfc2002b08f5aef0ea1008c43482019'),
}
UNIT_DIRECTORY = Path('/etc/systemd/system')
AUTH_UNIT = 'safe-code-desktop-device-auth.service'
FRP_UNIT = 'safe-code-desktop-frps.service'
PORT_CANDIDATES = range(45200, 45250)
MAX_ARCHIVE = 32 * 1024 * 1024
ARCHIVE_CACHE = Path('/var/cache/safe-code/frp')


def execute(*args, timeout=35):
    subprocess.run(args, check=True, timeout=timeout)


def download_frps(destination):
    target = ARCHIVES.get(platform.machine())
    if target is None:
        raise RuntimeError('當前 VPS 架構沒有配套的 FRP 安裝包。')
    arch, digest = target
    name = f'frp_{VERSION}_linux_{arch}'
    archive = None
    cached = ARCHIVE_CACHE/(name+'.tar.gz')
    if cached.is_file() and not cached.is_symlink() and cached.stat().st_size <= MAX_ARCHIVE:
        candidate = cached.read_bytes()
        if hashlib.sha256(candidate).hexdigest() == digest:
            archive = candidate
    if archive is None:
        url = f'https://github.com/fatedier/frp/releases/download/v{VERSION}/{name}.tar.gz'
        with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
            package = Path(folder)/'release.tar.gz'
            execute('/usr/bin/curl','-fsSL','--connect-timeout','12','--max-time','100',
                    '--max-filesize',str(MAX_ARCHIVE),'-o',str(package),url,timeout=105)
            if package.stat().st_size <= MAX_ARCHIVE:
                archive = package.read_bytes()
    if archive is None or hashlib.sha256(archive).hexdigest() != digest:
        raise RuntimeError('FRP 官方安裝包摘要校驗失敗。')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        member = bundle.getmember(name + '/frps')
        license_file = bundle.getmember(name + '/LICENSE')
        if not member.isfile() or not license_file.isfile():
            raise RuntimeError('FRP 官方安裝包缺少服務或許可證。')
        destination.write_bytes(bundle.extractfile(member).read())
        destination.chmod(0o755)
        destination.with_name('LICENSE').write_bytes(bundle.extractfile(license_file).read())


def select_ports():
    sockets = []
    try:
        ports = []
        for port in PORT_CANDIDATES:
            probe = socket.socket()
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                probe.close()
                continue
            sockets.append(probe)
            ports.append(port)
            if len(ports) == 2:
                return ports
        raise RuntimeError('沒有可用的私有網關裝置伺服器端口。')
    finally:
        for probe in sockets:
            probe.close()


def state_config(user, state, changes=None):
    arguments = ['runuser', '-u', user, '--', '/usr/bin/python3', '-B', __file__, '--state-config', str(state)]
    process = subprocess.run(arguments, input=json.dumps(changes), text=True,
                             capture_output=True, check=True, timeout=8)
    return json.loads(process.stdout)


def render_frp(config):
    return f'''bindAddr = "127.0.0.1"
bindPort = {config["deviceFrpPort"]}
proxyBindAddr = "127.0.0.1"
transport.tls.force = true
allowPorts = [{{start = {PORTS.start}, end = {PORTS.stop-1}}}]
log.to = "console"
log.level = "warn"
log.disablePrintColor = true

[[httpPlugins]]
name = "safecode-device-authorization"
addr = "127.0.0.1:{config["deviceAuthPort"]}"
path = "{config["deviceAuthCallback"]}"
ops = ["Login", "NewProxy", "Ping", "NewWorkConn", "NewUserConn"]
'''


def service_unit(user, description, command, *, after='network.target'):
    return f'''[Unit]
Description={description}
After={after}
[Service]
User={user}
ExecStart={command}
Restart=on-failure
RestartSec=2
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
'''


def file_digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024),b''):
            checksum.update(block)
    return checksum.hexdigest()


def write_private_config(path, content, user):
    write_file(path,content)
    os.chown(path,0,pwd.getpwnam(user).pw_gid)
    path.chmod(0o640)


def settle(config):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if device_service_problem(config) is None:
            return
        time.sleep(.25)
    raise RuntimeError('私有 FRP 或裝置鑑權回調未通過就緒檢查。')


def provision(user, app, state):
    if os.geteuid() != 0 or not re.fullmatch(r'[a-z_][a-z0-9_-]*',user) or user == 'root':
        raise ValueError('裝置服務需要管理員安裝並使用非 root 工作帳戶。')
    if any(not re.fullmatch(r'/[a-zA-Z0-9/_.-]+',str(path)) for path in (app,state,UNIT_DIRECTORY)):
        raise ValueError('裝置服務安裝路徑不受支援。')
    for directory in (app,app.parent,UNIT_DIRECTORY):
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=0 or directory.stat().st_mode&0o022:
            raise RuntimeError('裝置服務的程式或服務目錄權限異常。')
    current = state_config(user,state)
    component = app.parent/'desktop-device'
    manifest = component/'managed.json'
    auth, frp = [UNIT_DIRECTORY/name for name in (AUTH_UNIT,FRP_UNIT)]
    # Keep deployments that already point at an administrator-managed FRP.
    if current.get('deviceFrpPort') and not current.get('managedDeviceService'):
        return
    if manifest.is_file():
        if any(path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022
               for path in (component,manifest)):
            raise RuntimeError('裝置服務管理目錄權限異常。')
        saved = json.loads(manifest.read_text())
        if saved['user'] != user or saved['state'] != str(state):
            raise RuntimeError('裝置服務屬於不同的網關工作帳戶。')
        config = saved['config']
        units = saved['units']
        if current.get('managedDeviceService') and any(current.get(key)!=value for key,value in config.items()):
            raise RuntimeError('裝置服務配置已被修改，請核對監聽設定。')
        if any(path.is_symlink() or not path.is_file() or path.read_text()!=units[path.name] for path in (auth,frp)):
            raise RuntimeError('檢測到裝置服務單元已被修改，請核對 VPS 服務。')
        executable = component/'frps'
        frp_config = component/'frps.toml'
        callback = component/'callback'
        if any(path.is_symlink() for path in (executable,frp_config,callback)):
            raise RuntimeError('裝置服務組件路徑異常。')
        rebuilt = not executable.is_file() or file_digest(executable) != saved['binaryDigest']
        if rebuilt:
            with tempfile.TemporaryDirectory(dir=component) as folder:
                staged = Path(folder)/'frps'
                download_frps(staged)
                if file_digest(staged) != saved['binaryDigest']:
                    raise RuntimeError('FRP 可執行檔案與首次安裝的摘要不一致。')
                staged.replace(executable)
        configured = False
        for path,content in ((frp_config,render_frp(config)),(callback,config['deviceAuthCallback']+'\n')):
            if not path.is_file() or path.read_text() != content:
                write_private_config(path,content,user)
                configured = True
        auth_digest = file_digest(app/'device_bridge.py')
        auth_changed = auth_digest != saved.get('authDigest')
        if rebuilt or configured or device_service_problem(config) is not None:
            execute(str(executable),'verify','-c',str(frp_config))
            execute('systemctl','enable',auth.name,frp.name)
            execute('systemctl','restart',auth.name)
            execute('systemctl','restart',frp.name)
            settle(config)
        elif auth_changed:
            execute('systemctl','restart',auth.name)
            settle(config)
        if auth_changed:
            saved['authDigest'] = auth_digest
            write_file(manifest,json.dumps(saved))
        if not current.get('managedDeviceService'):
            state_config(user,state,config)
        return
    if current.get('managedDeviceService') or any(path.exists() or path.is_symlink() for path in (component,auth,frp)):
        raise RuntimeError('檢測到已有的裝置服務檔案，請核對 VPS 服務歸屬。')
    frp_port, auth_port = select_ports()
    config = dict(deviceFrpPort=frp_port,deviceAuthPort=auth_port,deviceAuthCallback='/'+secrets.token_hex(24),
                  managedDeviceService=True)
    config_file = component/'frps.toml'
    callback_file = component/'callback'
    units = {
        auth.name: service_unit(user,'Safe Code device authorization',
            f'/usr/bin/python3 -B {app}/device_bridge.py --state {state} --port {auth_port} --callback-file {callback_file}'
            ).replace('ProtectHome=read-only\n','ProtectHome=read-only\n'+f'ReadWritePaths={state}\n'),
        frp.name: service_unit(user,'Safe Code private device FRP',f'{component}/frps -c {config_file}',after=auth.name),
    }
    component.mkdir(mode=0o750)
    installed_units = []
    try:
        download_frps(component/'frps')
        os.chown(component,0,pwd.getpwnam(user).pw_gid)
        write_private_config(config_file,render_frp(config),user)
        write_private_config(callback_file,config['deviceAuthCallback']+'\n',user)
        manifest.write_text(json.dumps({'config':config,'units':units,'user':user,'state':str(state),
                                        'binaryDigest':file_digest(component/'frps'),
                                        'authDigest':file_digest(app/'device_bridge.py')}))
        manifest.chmod(0o600)
        execute(str(component/'frps'),'verify','-c',str(config_file))
        for path in (auth,frp):
            with path.open('x') as stream:
                installed_units.append(path)
                stream.write(units[path.name])
        execute('systemctl','daemon-reload')
        execute('systemctl','enable','--now',auth.name,frp.name)
        settle(config)
        state_config(user,state,config)
    except BaseException:
        for path in reversed(installed_units):
            execute('systemctl','disable','--now',path.name)
            path.unlink(missing_ok=True)
        if installed_units:
            execute('systemctl','daemon-reload')
        shutil.rmtree(component)
        raise


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--state-config' and os.geteuid() != 0:
        path = Path(sys.argv[2])/'config.json'
        config = json.loads(path.read_text())
        changes = json.load(sys.stdin)
        if changes is not None:
            config.update(changes)
            write_file(path,json.dumps(config))
        else:
            print(json.dumps({name:config.get(name) for name in (
                'deviceFrpPort','deviceAuthPort','deviceAuthCallback','managedDeviceService')}))
        if changes is not None:
            print('{}')
    elif len(sys.argv) == 4:
        provision(sys.argv[1],Path(sys.argv[2]),Path(sys.argv[3]))
    else:
        raise SystemExit('Usage: device_service.py linux-user gateway-program gateway-state')
