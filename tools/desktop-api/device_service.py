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
import time
from urllib.request import urlopen

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


def execute(*args, timeout=35):
    subprocess.run(args, check=True, timeout=timeout)


def download_frps(destination):
    target = ARCHIVES.get(platform.machine())
    if target is None:
        raise RuntimeError('当前 VPS 架构没有配套的 FRP 安装包。')
    arch, digest = target
    name = f'frp_{VERSION}_linux_{arch}'
    url = f'https://github.com/fatedier/frp/releases/download/v{VERSION}/{name}.tar.gz'
    with urlopen(url, timeout=35) as response:
        archive = response.read(MAX_ARCHIVE + 1)
    if len(archive) > MAX_ARCHIVE or hashlib.sha256(archive).hexdigest() != digest:
        raise RuntimeError('FRP 官方安装包摘要校验失败。')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        member = bundle.getmember(name + '/frps')
        license_file = bundle.getmember(name + '/LICENSE')
        if not member.isfile() or not license_file.isfile():
            raise RuntimeError('FRP 官方安装包缺少服务或许可证。')
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
        raise RuntimeError('没有可用的私有网关设备服务端口。')
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


def settle(config):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if device_service_problem(config) is None:
            return
        time.sleep(.25)
    raise RuntimeError('私有 FRP 或设备鉴权回调未通过就绪检查。')


def provision(user, app, state):
    if os.geteuid() != 0 or not re.fullmatch(r'[a-z_][a-z0-9_-]*',user) or user == 'root':
        raise ValueError('设备服务需要管理员安装并使用非 root 工作账户。')
    if any(not re.fullmatch(r'/[a-zA-Z0-9/_.-]+',str(path)) for path in (app,state,UNIT_DIRECTORY)):
        raise ValueError('设备服务安装路径不受支持。')
    current = state_config(user,state)
    component = app.parent/'desktop-device'
    manifest = component/'managed.json'
    auth, frp = [UNIT_DIRECTORY/name for name in (AUTH_UNIT,FRP_UNIT)]
    # Keep deployments that already point at an administrator-managed FRP.
    if current.get('deviceFrpPort') and not current.get('managedDeviceService'):
        return
    if manifest.is_file():
        if component.is_symlink() or manifest.is_symlink() or manifest.stat().st_uid!=0 or manifest.stat().st_mode&0o022:
            raise RuntimeError('设备服务管理目录权限异常。')
        saved = json.loads(manifest.read_text())
        config = saved['config']
        units = saved['units']
        if current.get('managedDeviceService') and any(current.get(key)!=value for key,value in config.items()):
            raise RuntimeError('设备服务配置已被修改，请核对监听设置。')
        if any(path.is_symlink() or not path.is_file() or path.read_text()!=units[path.name] for path in (auth,frp)):
            raise RuntimeError('检测到设备服务单元已被修改，请核对 VPS 服务。')
        if device_service_problem(config) is not None:
            execute('systemctl','enable',auth.name,frp.name)
            execute('systemctl','restart',auth.name)
            execute('systemctl','restart',frp.name)
            settle(config)
        if not current.get('managedDeviceService'):
            state_config(user,state,config)
        return
    if current.get('managedDeviceService') or any(path.exists() or path.is_symlink() for path in (component,auth,frp)):
        raise RuntimeError('检测到已有的设备服务文件，请核对 VPS 服务归属。')
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
        config_file.write_text(render_frp(config))
        callback_file.write_text(config['deviceAuthCallback']+'\n')
        work_gid = pwd.getpwnam(user).pw_gid
        os.chown(component,0,work_gid)
        for path in (config_file,callback_file):
            os.chown(path,0,work_gid)
            path.chmod(0o640)
        manifest.write_text(json.dumps({'config':config,'units':units}))
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
