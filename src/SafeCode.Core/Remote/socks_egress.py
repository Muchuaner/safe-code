"""Experimental Ubuntu SOCKS5 egress with an independent nftables guard."""
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import pwd
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

STATE = Path('/var/lib/safe-code')
INSTALL = Path('/opt/safe-code/socks-egress')
UNIT_DIR = Path('/etc/systemd/system')
STATE_FILE = 'socks-egress.json'
GUARD = 'safe-code-socks-guard.service'
RELAY = 'safe-code-socks-relay.service'
OWNER = 'Safe Code SOCKS5 egress'
USER = 'safe-code-socks'
TUN = 'safecode-socks'
TABLE = 'safe_code_socks'
ROUTE_TABLE = '37242'
RULE_PRIORITY = '170'
MARK = '0x40000000'
IP_CHECK = 'api.ipify.org'
DNS = '1.1.1.1'
VERSION = '1.14.4'
DOWNLOADS = {
    'x86_64': ('amd64', '699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968'),
    'aarch64': ('arm64', '436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26'),
}
GUARD_UNIT = f"""[Unit]
Description={OWNER} guard
DefaultDependencies=no
After=local-fs.target nftables.service
Before=network-pre.target shutdown.target
Wants=network-pre.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {INSTALL}/socks_egress.py guard

[Install]
WantedBy=network-pre.target multi-user.target
"""
RELAY_UNIT = f"""[Unit]
Description={OWNER} relay
Requires={GUARD}
After={GUARD} network-online.target
Wants=network-online.target

[Service]
Type=exec
User={USER}
Group={USER}
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
ExecStart={INSTALL}/sing-box run -c {INSTALL}/relay.json
ExecStartPost=+/usr/bin/python3 {INSTALL}/socks_egress.py routes
Restart=always
RestartSec=3s

[Install]
WantedBy=multi-user.target
"""


def run(args, *, check=True, stdin=None, timeout=20):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} 执行失败，请检查服务器网络组件。')
    return result


def save(path, contents, mode=0o600, gid=0):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            os.fchown(handle.fileno(), 0, gid)
            os.fchmod(handle.fileno(), mode)
            handle.write(contents.encode())
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def parse_proxy(url):
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 2048 or re.search(r'\s|%(?![0-9a-fA-F]{2})', url):
            raise ValueError()
        parts = urlsplit(url)
        address = ipaddress.IPv4Address(parts.hostname or '')
        if (parts.scheme != 'socks5' or not parts.port or parts.path not in ('', '/') or parts.query or parts.fragment
                or not address.is_global or address.is_multicast or parts.netloc.count('@') > 1):
            raise ValueError()
        username = unquote(parts.username or '', errors='strict')
        password = unquote(parts.password or '', errors='strict')
        if (parts.username is not None and not (username and password)
                or any(len(x.encode()) > 255 or re.search(r'[\x00-\x1f\x7f]', x) for x in (username, password))):
            raise ValueError()
        return {'address': str(address), 'port': parts.port, 'username': username, 'password': password}
    except (ValueError, TypeError):
        raise ValueError('请输入有效的 SOCKS5 地址，使用公网 IPv4 和端口，有认证时填写用户名与密码。') from None


def receive(sock, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)
        piece = sock.recv(size - len(data))
        if not piece:
            raise OSError('SOCKS5 代理中断了连接。')
        data.extend(piece)
    return bytes(data)


def request_proxy(sock, proxy, command, destination, port):
    deadline = time.monotonic() + 5

    def read(size):
        return receive(sock, size, deadline)

    username, password = (proxy[key].encode() for key in ('username', 'password'))
    method = 2 if username else 0
    sock.sendall(bytes((5, 1, method)))
    if read(2) != bytes((5, method)):
        raise OSError('代理不接受所选 SOCKS5 认证方式。')
    if username:
        sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
        if read(2) != b'\x01\x00':
            raise OSError('SOCKS5 用户名或密码未通过认证。')
    try:
        address = b'\x01' + socket.inet_aton(destination)
    except OSError:
        name = destination.encode('idna')
        address = bytes((3, len(name))) + name
    sock.sendall(bytes((5, command, 0)) + address + struct.pack('!H', port))
    version, reply, reserved, kind = read(4)
    if (version, reply, reserved) != (5, 0, 0):
        raise OSError('代理拒绝了 SOCKS5 转发请求。')
    if kind == 1:
        bound = socket.inet_ntoa(read(4))
    elif kind == 3:
        bound = read(read(1)[0]).decode('ascii')
    elif kind == 4:
        bound = socket.inet_ntop(socket.AF_INET6, read(16))
    else:
        raise OSError('代理返回了无效的 SOCKS5 地址。')
    return bound, struct.unpack('!H', read(2))[0]


def tcp_probe(proxy):
    with socket.create_connection((proxy['address'], proxy['port']), timeout=5) as control:
        request_proxy(control, proxy, 1, IP_CHECK, 443)
        control.settimeout(5)
        with ssl.create_default_context().wrap_socket(control, server_hostname=IP_CHECK) as encrypted:
            encrypted.sendall(f'GET / HTTP/1.1\r\nHost: {IP_CHECK}\r\nConnection: close\r\n\r\n'.encode())
            response = http.client.HTTPResponse(encrypted)
            response.begin()
            if response.status != 200:
                raise OSError('出口检测服务未返回成功。')
            return str(ipaddress.IPv4Address(response.read(64).decode().strip()))


def dns_probe(relay=None):
    nonce = os.urandom(2)
    query = nonce + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01'
    header = b'\0\0\0\x01' + socket.inet_aton(DNS) + struct.pack('!H', 53) if relay else b''
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(5)
        udp.connect(relay or (DNS, 53))
        udp.send(header + query)
        packet = udp.recv(4096)
    if not packet.startswith(header):
        raise OSError('UDP 中继返回了不匹配的目标地址。')
    answer = packet[len(header):]
    if (len(answer) < len(query) or answer[:2] != nonce or answer[2] & 0x82 != 0x80
            or answer[3] & 15 or answer[6:8] == b'\0\0' or answer[12:len(query)] != query[12:]):
        raise OSError('未收到有效的 UDP DNS 应答。')


def udp_probe(proxy):
    with socket.create_connection((proxy['address'], proxy['port']), timeout=5) as control:
        relay, port = request_proxy(control, proxy, 3, '0.0.0.0', 0)
        if relay not in ('0.0.0.0', proxy['address']) or not port:
            raise OSError('UDP 中继需要与 SOCKS5 接入地址使用同一个 IPv4。')
        dns_probe((proxy['address'], port))


def probe(proxy):
    result = {'tcp': False, 'udp': False, 'observedIp': ''}
    for protocol, test in (('tcp', tcp_probe), ('udp', udp_probe)):
        try:
            value = test(proxy)
            result[protocol] = True
            if protocol == 'tcp':
                result['observedIp'] = value
        except TimeoutError:
            result[protocol + 'Message'] = '等待代理转发超时。'
        except (OSError, ValueError, http.client.HTTPException) as error:
            known = ('代理', 'SOCKS5', '出口检测', '未收到', 'UDP 中继')
            message = str(error)
            result[protocol + 'Message'] = message if message.startswith(known) else '未收到可验证的转发响应。'
    return result


def load_state():
    path = STATE / STATE_FILE
    return json.loads(path.read_text()) if path.exists() else None


def existing_guard():
    result = run(['nft', '-j', 'list', 'table', 'inet', TABLE], check=False)
    if result.returncode:
        if 'No such file or directory' in result.stderr:
            return False
        raise RuntimeError('无法检查 SOCKS5 防火墙规则。')
    tables = json.loads(result.stdout)['nftables']
    if not any(item.get('table', {}).get('comment') == OWNER for item in tables):
        raise RuntimeError('发现同名防火墙表，请由管理员确认。')
    return True


def status():
    state = load_state()
    if not state:
        return {'enabled': False}
    try:
        guarded = existing_guard()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
        guarded = False
    running = run(['systemctl', 'is-active', '--quiet', RELAY], check=False).returncode == 0
    observed = state.get('observedIp', '')
    return {'enabled': True, 'guardActive': guarded, 'tunnelRunning': running,
            'endpoint': f"{state['address']}:{state['port']}", 'observedIp': observed,
            'issue': '' if observed else '整机出站尚未通过验证。'}


def guard_rules(state):
    # Only postrouting sees the output device after the route hook changes the mark.
    proxy_flow = f"""meta skuid {int(state['uid'])} ip daddr {state['address']} tcp dport {int(state['port'])} return
        meta skuid {int(state['uid'])} ip daddr {state['address']} meta l4proto udp return"""
    link_traffic = """ip daddr 255.255.255.255 udp sport 68 udp dport 67 return
        ip6 daddr { fe80::/10, ff02::/16 } icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } return"""
    return f"""table inet {TABLE} {{
    comment "{OWNER}"
    chain steer_host {{
        type route hook output priority -90; policy accept;
        oifname "lo" return
        ct direction reply return
        {proxy_flow}
        {link_traffic}
        meta l4proto {{ tcp, udp }} meta mark set meta mark | {MARK}
    }}
    chain steer_forward {{
        type filter hook prerouting priority -90; policy accept;
        iifname {{ "lo", "{TUN}" }} return
        ct direction reply return
        ct status dnat return
        fib daddr type local return
        meta l4proto {{ tcp, udp }} meta mark set meta mark | {MARK}
    }}
    chain guard_egress {{
        type filter hook postrouting priority 110; policy accept;
        oifname "lo" return
        ct direction reply return
        {proxy_flow}
        {link_traffic}
        ct status dnat oifname {{ "docker0", "br-*" }} return
        oifname "{TUN}" meta l4proto {{ tcp, udp }} return
        counter drop
    }}
}}
"""


def guard_batch(state):
    delete = f'delete table inet {TABLE}\n' if existing_guard() else ''
    return delete + guard_rules(state)


def routing_rules(family):
    reply = run(['ip', family, '-j', 'rule', 'show'])
    return json.loads(reply.stdout)


def own_route_rule(rule):
    return (str(rule.get('priority')) == RULE_PRIORITY and str(rule.get('table')) == ROUTE_TABLE
            and rule.get('fwmark') == MARK and rule.get('fwmask') == MARK)


def preflight_routing():
    if run(['ip', 'link', 'show', 'dev', TUN], check=False).returncode == 0:
        raise RuntimeError('专用隧道接口名称已被占用。')
    for family in ('-4', '-6'):
        if any(str(rule.get('priority')) == RULE_PRIORITY or str(rule.get('table')) == ROUTE_TABLE
               for rule in routing_rules(family)):
            raise RuntimeError('专用路由表或策略优先级已被占用。')
        routes = run(['ip', family, '-j', 'route', 'show', 'table', ROUTE_TABLE], check=False)
        if routes.returncode == 0 and json.loads(routes.stdout):
            raise RuntimeError('专用路由表已有其他路由。')
        if routes.returncode and 'FIB table does not exist' not in routes.stderr:
            raise RuntimeError('无法检查专用路由表。')


def ensure_rules():
    for family in ('-4', '-6'):
        rules = [rule for rule in routing_rules(family) if str(rule.get('priority')) == RULE_PRIORITY]
        if rules and not (len(rules) == 1 and own_route_rule(rules[0])):
            raise RuntimeError('专用路由策略优先级已被占用。')
        if not rules:
            run(['ip', family, 'rule', 'add', 'priority', RULE_PRIORITY, 'fwmark', f'{MARK}/{MARK}', 'lookup', ROUTE_TABLE])


def start_guard():
    state = load_state()
    if not state:
        raise RuntimeError('SOCKS5 出口未配置。')
    run(['nft', '--file', '-'], stdin=guard_batch(state))
    ensure_rules()


def tunnel_routes():
    for _ in range(50):
        state = run(['ip', '-j', 'link', 'show', 'dev', TUN], check=False)
        if state.returncode == 0 and 'UP' in json.loads(state.stdout)[0]['flags']:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('未能启动 SOCKS5 隧道网卡。')
    Path(f'/proc/sys/net/ipv4/conf/{TUN}/rp_filter').write_text('2\n')
    for family in ('-4', '-6'):
        run(['ip', family, 'route', 'replace', 'table', ROUTE_TABLE, 'default', 'dev', TUN])


def relay_config(proxy):
    return {
        'log': {'disabled': True},
        'inbounds': [{'type': 'tun', 'tag': 'tun', 'interface_name': TUN, 'stack': 'gvisor',
                      'address': ['198.18.255.253/32', 'fd7a:5343:736f:636b::1/128'],
                      'mtu': 1400, 'auto_route': False, 'dns_mode': 'disabled'}],
        'outbounds': [{'type': 'socks', 'tag': 'proxy', 'server': proxy['address'], 'server_port': proxy['port'],
                       'version': '5', 'username': proxy['username'], 'password': proxy['password']}],
        'route': {'rules': [{'port': 53, 'action': 'route-options', 'override_address': DNS}], 'final': 'proxy'},
    }


def download_relay():
    release = DOWNLOADS.get(platform.machine())
    if not release:
        raise RuntimeError('SOCKS5 开发模式支持 x86_64 和 ARM64 服务器。')
    arch, digest = release
    tag = f'sing-box-{VERSION}-linux-{arch}'
    with tempfile.TemporaryDirectory(dir=INSTALL) as scratch:
        archive = Path(scratch) / 'sing-box.tar.gz'
        try:
            run(['curl', '--noproxy', '*', '--proto', '=https', '--proto-redir', '=https', '-fLsS',
                 '--connect-timeout', '8', '--max-time', '120', '--retry', '2', '--retry-max-time', '130',
                 '-o', str(archive), f'https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/{tag}.tar.gz'],
                timeout=255)
        except (RuntimeError, subprocess.TimeoutExpired):
            raise RuntimeError('下载 SOCKS5 隧道组件失败，请检查服务器是否能访问 GitHub Releases。') from None
        checksum = hashlib.sha256()
        with archive.open('rb') as packed:
            for chunk in iter(lambda: packed.read(1024 * 1024), b''):
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise RuntimeError('SOCKS5 隧道组件校验失败。')
        with tarfile.open(archive, 'r:gz') as package:
            entry = package.getmember(f'{tag}/sing-box')
            if not entry.isfile():
                raise RuntimeError('SOCKS5 隧道组件格式无效。')
            executable = Path(scratch) / 'sing-box'
            with package.extractfile(entry) as source, executable.open('xb') as target:
                shutil.copyfileobj(source, target)
            executable.chmod(0o755)
            os.replace(executable, INSTALL / 'sing-box')


def relay_user():
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent',
             '--shell', '/usr/sbin/nologin', '--comment', OWNER, USER])
        user = pwd.getpwnam(USER)
    if not user.pw_uid or user.pw_gecos != OWNER or user.pw_shell != '/usr/sbin/nologin':
        raise RuntimeError('专用隧道账户名称已被占用。')
    return user


def ensure_idle():
    if not Path('/dev/net/tun').exists():
        raise RuntimeError('服务器未提供 /dev/net/tun，请先启用 TUN 设备。')
    if load_state():
        raise RuntimeError('请先停用已配置的 SOCKS5 出口。')
    if any(STATE.glob('ip-pending-*.json')):
        raise RuntimeError('已有出入口 IP 变更等待验证，请先完成该操作。')
    if existing_guard() or any((UNIT_DIR / name).exists() for name in (GUARD, RELAY)):
        raise RuntimeError('发现未完成清理的 SOCKS5 配置，请检查服务器状态。')
    policy_file = STATE / 'ip-policy.json'
    if policy_file.exists() and json.loads(policy_file.read_text()).get('lockEgress'):
        raise RuntimeError('请先在「更换 IP」中取消固定住宅出口，再启用 SOCKS5。')
    preflight_routing()


def observe():
    response = run(['curl', '--noproxy', '*', '-4', '-fsS', '--connect-timeout', '5', '--max-time', '12',
                    f'https://{IP_CHECK}']).stdout.strip()
    return str(ipaddress.IPv4Address(response))


def enable(proxy):
    ensure_idle()
    checked = probe(proxy)
    if not (checked['tcp'] and checked['udp']):
        raise RuntimeError('TCP 或 UDP 复检未通过，出站规则尚未启用。请重新检测代理。')
    INSTALL.mkdir(mode=0o755, exist_ok=True)
    download_relay()
    user = relay_user()
    state = {'address': proxy['address'], 'port': proxy['port'], 'uid': user.pw_uid}
    batch = guard_batch(state)
    run(['nft', '--check', '--file', '-'], stdin=batch)
    save(INSTALL / 'relay.json', json.dumps(relay_config(proxy)), 0o640, user.pw_gid)
    try:
        run([str(INSTALL / 'sing-box'), 'check', '-c', str(INSTALL / 'relay.json')])
        save(INSTALL / 'socks_egress.py', Path(__file__).read_text(), 0o644)
        save(UNIT_DIR / GUARD, GUARD_UNIT, 0o644)
        save(UNIT_DIR / RELAY, RELAY_UNIT, 0o644)
        save(STATE / STATE_FILE, json.dumps(state))
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', '--now', GUARD])
        run(['systemctl', 'enable', '--now', RELAY], timeout=40)
        dns_probe()
        state['observedIp'] = observe()
        save(STATE / STATE_FILE, json.dumps(state))
        return status()
    except Exception:
        if existing_guard():
            current = status()
            return {**current, 'issue': '隧道未通过整机出站验证。阻断规则保持启用，请排查代理或手动停用。'}
        (STATE / STATE_FILE).unlink(missing_ok=True)
        (INSTALL / 'relay.json').unlink(missing_ok=True)
        for name in (RELAY, GUARD):
            run(['systemctl', 'disable', name], check=False)
            (UNIT_DIR / name).unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'], check=False)
        raise RuntimeError('未能启用 SOCKS5 出口，请检查服务器网络组件。') from None


def disable():
    if not load_state():
        return {'enabled': False}
    guarded = existing_guard()
    run(['systemctl', 'disable', '--now', RELAY])
    run(['systemctl', 'disable', '--now', GUARD])
    try:
        for family in ('-4', '-6'):
            if any(own_route_rule(rule) for rule in routing_rules(family)):
                run(['ip', family, 'rule', 'del', 'priority', RULE_PRIORITY,
                     'fwmark', f'{MARK}/{MARK}', 'lookup', ROUTE_TABLE])
        if guarded:
            run(['nft', 'delete', 'table', 'inet', TABLE])
    except Exception:
        run(['systemctl', 'enable', '--now', GUARD], check=False)
        run(['systemctl', 'enable', '--now', RELAY], check=False)
        raise
    (STATE / STATE_FILE).unlink(missing_ok=True)
    (INSTALL / 'relay.json').unlink(missing_ok=True)
    (UNIT_DIR / RELAY).unlink(missing_ok=True)
    (UNIT_DIR / GUARD).unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    return {'enabled': False}


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('需要 root 权限。')
    {'guard': start_guard, 'routes': tunnel_routes}[sys.argv[1]]()
