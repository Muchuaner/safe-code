#!/usr/bin/env python3
"""Run inside a disposable Ubuntu systemd container with --network none, never on a host VPS.

Usage: python3 test_socks_egress_namespace.py /path/to/socks_egress.py /path/to/pinned/sing-box
"""
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

HOST = '198.18.45.10'
PROXY = '198.18.45.1'
PROXY_EGRESS = '198.18.46.1'
ORIGIN = '198.51.100.20'
ORIGIN6 = '2001:db8:46::20'
USERNAME = b'fixture-user'
PASSWORD = b'fixture-password'


def read(sock, size):
    with sock.makefile('rb', buffering=0) as stream:
        data = stream.read(size)
    if len(data) != size:
        raise OSError('short read')
    return data


def address(kind, reader):
    if kind == 1:
        return socket.inet_ntoa(reader(4))
    if kind == 4:
        return socket.inet_ntop(socket.AF_INET6, reader(16))
    if kind == 3:
        return reader(reader(1)[0]).decode()
    raise OSError('unexpected fixture address type')


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(6)
        try:
            version, count = read(sock, 2)
            if version != 5 or 2 not in read(sock, count):
                return
            sock.sendall(b'\x05\x02')
            version, size = read(sock, 2)
            name = read(sock, size)
            password = read(sock, read(sock, 1)[0])
            if (version, name, password) != (1, USERNAME, PASSWORD):
                sock.sendall(b'\x01\x01')
                return
            sock.sendall(b'\x01\x00')
            version, command, _, kind = read(sock, 4)
            target = address(kind, lambda size: read(sock, size))
            port = struct.unpack('!H', read(sock, 2))[0]
            if version == 5 and command == 1:
                self.tcp(sock, target, port)
            elif version == 5 and command == 3:
                self.udp(sock)
        except OSError:
            return

    def tcp(self, sock, target, port):
        with socket.create_connection((target, port), timeout=5) as outbound:
            sock.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)
            threading.Thread(target=self.copy, args=(sock, outbound), daemon=True).start()
            self.copy(outbound, sock)

    @staticmethod
    def copy(source, destination):
        try:
            while data := source.recv(16384):
                destination.sendall(data)
        except OSError:
            pass

    def udp(self, sock):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as relay:
            relay.bind((PROXY, 0))
            relay.settimeout(5)
            sock.sendall(b'\x05\x00\x00\x01' + socket.inet_aton(PROXY) + struct.pack('!H', relay.getsockname()[1]))
            while True:
                packet, client = relay.recvfrom(4096)
                if packet[:3] != b'\x00\x00\x00':
                    return
                message = io.BytesIO(packet[4:])
                target = address(packet[3], message.read)
                port = struct.unpack('!H', message.read(2))[0]
                header = packet[:4 + message.tell()]
                family = socket.AF_INET6 if ':' in target else socket.AF_INET
                with socket.socket(family, socket.SOCK_DGRAM) as outbound:
                    outbound.settimeout(3)
                    outbound.connect((target, port))
                    outbound.send(message.read())
                    relay.sendto(header + outbound.recv(4096), client)


class SocksServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        source = self.client_address[0].encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(source)))
        self.end_headers()
        self.wfile.write(source)

    def log_message(self, *args):
        pass


def serve_origin(certificate, key):
    def dns():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind((ORIGIN, 53))
            while True:
                query, peer = udp.recvfrom(4096)
                answer = query[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + query[12:]
                answer += b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x1e\x00\x04' + socket.inet_aton(peer[0])
                udp.sendto(answer, peer)

    class EchoV6(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

    threading.Thread(target=dns, daemon=True).start()
    v6 = EchoV6((ORIGIN6, 8080), EchoHandler)
    threading.Thread(target=v6.serve_forever, daemon=True).start()
    server = http.server.ThreadingHTTPServer((ORIGIN, 443), EchoHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=20)


def ip(*args):
    run('ip', *args)


def ns(namespace, *args):
    return ('ip', 'netns', 'exec', namespace, *args)


def curl(*args):
    return ('curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1', '--max-time', '2', *args)


def child(*args):
    return subprocess.Popen(args, stdout=subprocess.DEVNULL)


def source(*args):
    reply = run(*args, check=False)
    if reply.returncode:
        raise AssertionError(reply.stderr)
    return reply.stdout.strip()


def udp_source():
    request = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect((ORIGIN, 53))
        client.send(request)
        return socket.inet_ntoa(client.recv(4096)[-4:])


def stop(process):
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=3)


def test(module_path, binary):
    if os.environ.get('container') not in ('docker', 'oci') and not Path('/.dockerenv').exists():
        raise SystemExit('Use a disposable container.')
    if len(json.loads(run('ip', '-j', 'route', 'show', 'default').stdout)):
        raise SystemExit('Start the test container with --network none.')
    spec = importlib.util.spec_from_file_location('policy', module_path)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    fixtures = Path(__file__).resolve()
    policy.IP_CHECK = policy.DNS = ORIGIN
    policy.STATE.mkdir(mode=0o700, exist_ok=True)
    policy.INSTALL.parent.mkdir(mode=0o755, exist_ok=True)
    proxy = {'address': PROXY, 'port': 1080, 'username': USERNAME.decode(), 'password': PASSWORD.decode()}
    assert policy.parse_proxy('socks5://fixture%40example:pass%3Aword@1.1.1.1:443')['username'] == 'fixture@example'
    for invalid in ('http://1.1.1.1:443', 'socks5://localhost:443', 'socks5://1.1.1.1', 'socks5://u:%zz@1.1.1.1:443'):
        try:
            policy.parse_proxy(invalid)
        except ValueError:
            continue
        raise AssertionError('invalid URL accepted')

    def use_pinned_relay():
        shutil.copy2(binary, policy.INSTALL / 'sing-box')

    policy.download_relay = use_pinned_relay
    with tempfile.TemporaryDirectory() as scratch:
        certificate = Path(scratch) / 'local-ca.crt'
        key = Path(scratch) / 'local-ca.key'
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=local fixture',
            '-addext', f'subjectAltName=IP:{ORIGIN}', '-keyout', str(key), '-out', str(certificate))
        os.environ['SSL_CERT_FILE'] = os.environ['CURL_CA_BUNDLE'] = str(certificate)
        processes = []
        try:
            ip('netns', 'add', 'sc-peer')
            ip('netns', 'add', 'sc-origin')
            ip('link', 'add', 'sc-host', 'type', 'veth', 'peer', 'name', 'sc-entry')
            ip('link', 'set', 'sc-entry', 'netns', 'sc-peer')
            ip('link', 'add', 'sc-egress', 'type', 'veth', 'peer', 'name', 'sc-back')
            ip('link', 'set', 'sc-egress', 'netns', 'sc-peer')
            ip('link', 'set', 'sc-back', 'netns', 'sc-origin')
            for namespace, name, addresses in (
                ('', 'sc-host', (HOST + '/24', '2001:db8:45::10/64')),
                ('sc-peer', 'sc-entry', (PROXY + '/24', '2001:db8:45::1/64')),
                ('sc-peer', 'sc-egress', (PROXY_EGRESS + '/24', '2001:db8:46::1/64')),
                ('sc-origin', 'sc-back', ('198.18.46.20/24', ORIGIN6 + '/64')),
            ):
                prefix = ('-n', namespace) if namespace else ()
                ip(*prefix, 'link', 'set', 'lo', 'up')
                ip(*prefix, 'link', 'set', name, 'up')
                for literal in addresses:
                    ip(*prefix, 'addr', 'add', literal, 'dev', name, *(['nodad'] if ':' in literal else []))
            ip('-n', 'sc-origin', 'addr', 'add', ORIGIN + '/32', 'dev', 'lo')
            ip('-n', 'sc-peer', 'route', 'add', ORIGIN + '/32', 'via', '198.18.46.20')
            for prefix, via, peer_via in (
                ((), PROXY, PROXY_EGRESS),
                (('-6',), '2001:db8:45::1', '2001:db8:46::1'),
            ):
                ip(*prefix, 'route', 'add', 'default', 'via', via)
                ip('-n', 'sc-origin', *prefix, 'route', 'add', 'default', 'via', peer_via)
            run(*ns('sc-peer', 'sysctl', '-w', 'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1'))
            helper = (sys.executable, str(fixtures))
            start_proxy = ns('sc-peer', *helper, 'proxy')
            processes.append(child(*ns('sc-origin', *helper, 'origin', str(certificate), str(key))))
            processes.append(child(*helper, 'ingress'))
            proxy_process = child(*start_proxy)
            processes.append(proxy_process)
            for _ in range(50):
                listening = run(*ns('sc-origin', 'ss', '-ltn', 'sport = :443')).stdout
                if str(ORIGIN) + ':443' in listening:
                    break
                if processes[0].poll() is not None:
                    raise AssertionError('Origin fixture exited')
                time.sleep(0.1)
            else:
                raise AssertionError('Origin fixture never opened HTTPS')
            assert source(*curl(f'https://{ORIGIN}')) == HOST
            assert udp_source() == HOST
            assert source(*curl(f'http://[{ORIGIN6}]:8080')) == '2001:db8:45::10'
            ingress = ns('sc-peer', *curl(f'http://{HOST}:22222'))
            assert source(*ingress) == PROXY

            bad = policy.probe({**proxy, 'password': 'invalid'})
            assert not bad['tcp'] and not bad['udp']
            checked = policy.probe(proxy)
            assert checked == {'tcp': True, 'udp': True, 'observedIp': PROXY_EGRESS}, checked
            enabled = policy.enable(proxy)
            assert enabled['guardActive'] and enabled['tunnelRunning'] and not enabled['issue'], enabled
            assert policy.observe() == PROXY_EGRESS
            assert source(*curl(f'http://[{ORIGIN6}]:8080')) == '2001:db8:46::1'
            assert source(*ingress) == PROXY
            policy.dns_probe()
            assert udp_source() == PROXY_EGRESS

            stop(proxy_process)
            assert run(*curl(f'https://{ORIGIN}'), check=False).returncode
            assert run(*curl(f'http://[{ORIGIN6}]:8080'), check=False).returncode
            assert source(*ingress) == PROXY
            try:
                udp_source()
            except (TimeoutError, ConnectionRefusedError):
                pass
            else:
                raise AssertionError('UDP fell back to the physical network')
            run('systemctl', 'stop', policy.RELAY)
            assert policy.status()['guardActive']
            assert run(*curl(f'https://{ORIGIN}'), check=False).returncode
            assert run(*curl('--interface', 'sc-host', f'https://{ORIGIN}'), check=False).returncode
            assert source(*ingress) == PROXY
            proxy_process = child(*start_proxy)
            processes.append(proxy_process)
            run('systemctl', 'start', policy.RELAY)
            assert policy.observe() == PROXY_EGRESS
            policy.dns_probe()

            assert policy.disable() == {'enabled': False}
            assert policy.observe() == HOST
            assert udp_source() == HOST
            assert not policy.existing_guard()
            assert not (policy.INSTALL / 'relay.json').exists()
            print(json.dumps({'probe': checked, 'tcpIpv4AndIpv6': True, 'udp': True, 'ingressPreserved': True,
                              'proxyAndTunnelFailureBlocked': True, 'tunnelRestartRecovered': True, 'disabledRestored': True}))
        finally:
            if policy.load_state():
                policy.disable()
            for process in processes:
                stop(process)
            for namespace in ('sc-origin', 'sc-peer'):
                run('ip', 'netns', 'del', namespace, check=False)


if __name__ == '__main__':
    if sys.argv[1] == 'origin':
        serve_origin(*sys.argv[2:])
    elif sys.argv[1] == 'proxy':
        with SocksServer((PROXY, 1080), SocksHandler) as server:
            server.serve_forever()
    elif sys.argv[1] == 'ingress':
        http.server.ThreadingHTTPServer((HOST, 22222), EchoHandler).serve_forever()
    else:
        test(*sys.argv[1:])
