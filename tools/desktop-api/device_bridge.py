"""SafeCode device enrolment and the private FRP authorization callback."""
import argparse
from contextlib import closing
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import sqlite3
import subprocess
import tempfile
import time


PROOF = re.compile(r'[A-Za-z0-9_-]{43}')
DEVICE = re.compile(r'[0-9a-f]{20}')
KEY_ID = re.compile(r'[0-9a-f]{64}')
PORTS = range(45300, 45332)
MAX_JSON = 4096
LEASE_SECONDS = 120


class DeviceError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_file(path, content):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


class Devices:
    def __init__(self, state, *, home=None):
        self.state = Path(state)
        self.home = Path.home() if home is None else Path(home)
        self.database = self.state/'device-bridge.sqlite3'
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, proof TEXT NOT NULL,
                platform TEXT NOT NULL, hostname TEXT NOT NULL, alias TEXT NOT NULL UNIQUE,
                port INTEGER NOT NULL UNIQUE, expires INTEGER NOT NULL, ready INTEGER NOT NULL DEFAULT 0,
                username TEXT, hostkey TEXT)''')

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    def permitted(self, owner):
        return bool(KEY_ID.fullmatch(owner) and (self.state/'keys'/owner).is_file()
                    and (self.state/'device-access'/owner).is_file())

    def required(self, owner):
        return bool(KEY_ID.fullmatch(owner) and (self.state/'device-required'/owner).is_file())

    def ssh_folder(self, device):
        return self.home/'.ssh/safe-code-devices'/device

    @staticmethod
    def proof_matches(row, proof):
        return (isinstance(proof, str) and PROOF.fullmatch(proof)
                and hmac.compare_digest(row['proof'], digest(proof)))

    def enrolled(self, owner, platform, device, proof):
        if not isinstance(device, str) or not DEVICE.fullmatch(device) or not self.permitted(owner):
            raise DeviceError('Device pairing is unavailable')
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM devices WHERE id=? AND owner=? AND platform=?',
                             (device, owner, platform)).fetchone()
        if row is None or not self.proof_matches(row, proof):
            raise DeviceError('Unknown device pairing')
        return row

    def prepare(self, owner, platform, hostname):
        if not self.permitted(owner):
            raise DeviceError('Device pairing is unavailable')
        if (platform not in ('macos', 'windows') or not isinstance(hostname, str)
            or not re.fullmatch(r'[\w .-]{1,48}', hostname) or not hostname.strip()):
            raise DeviceError('Invalid device name')
        device = secrets.token_hex(10)
        alias = ('win-' if platform == 'windows' else 'mac-')+device
        proof = secrets.token_urlsafe(32)
        folder = self.ssh_folder(device)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('SELECT * FROM devices WHERE owner=? AND ready=0 AND expires<=?',
                                 (owner, int(time.time()))).fetchall()
            for row in pending:
                self.remove_identity(row['id'])
                db.execute('DELETE FROM devices WHERE id=?', (row['id'],))
            if db.execute('SELECT COUNT(*) FROM devices WHERE owner=?', (owner,)).fetchone()[0] >= 8:
                raise DeviceError('Device pairing capacity reached for this Key')
            occupied = {row[0] for row in db.execute('SELECT port FROM devices')}
            port = next((p for p in PORTS if p not in occupied), None)
            if port is None:
                raise DeviceError('Device pairing capacity reached')
            folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.mkdir(mode=0o700)
            key = folder/'identity'
            try:
                subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                               capture_output=True, check=True, timeout=8)
                public = key.with_suffix('.pub').read_text().strip().split()
                db.execute('''INSERT INTO devices(id,owner,proof,platform,hostname,alias,port,expires)
                              VALUES (?,?,?,?,?,?,?,?)''',
                           (device, owner, digest(proof), platform, hostname.strip(), alias, port,
                            int(time.time())+LEASE_SECONDS))
            except (OSError, subprocess.SubprocessError):
                for name in ('identity', 'identity.pub'):
                    (folder/name).unlink(missing_ok=True)
                folder.rmdir()
                raise DeviceError('Could not prepare the device identity') from None
        return {'deviceId': device, 'deviceProof': proof, 'alias': alias, 'port': port,
                'publicKey': ' '.join(public[:2]), 'proxyName': alias}

    def configure(self, row, username, key):
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', username)
            or not isinstance(key, str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]{40,128}', key)):
            raise DeviceError('Invalid SSH login or host public key')
        if row['ready']:
            if row['username'] != username or row['hostkey'] != key:
                raise DeviceError('Device SSH identity changed; pair it again')
        if row['expires'] <= time.time():
            raise DeviceError('Device pairing expired before verification')
        folder = self.ssh_folder(row['id'])
        pin = 'safecode-device-'+row['id']
        config = folder/'config'
        known = folder/'known_hosts'
        write_file(known, pin+' '+key+'\n')
        write_file(config, f'''Host {row["alias"]}
  HostName 127.0.0.1
  Port {row["port"]}
  User {username}
  IdentityFile "{folder/'identity'}"
  UserKnownHostsFile "{known}"
  GlobalKnownHostsFile /dev/null
  HostKeyAlias {pin}
  HostKeyAlgorithms ssh-ed25519
  StrictHostKeyChecking yes
  IdentitiesOnly yes
  IdentityAgent none
  BatchMode yes
  ForwardAgent no
  ClearAllForwardings yes
  ProxyCommand none
  PermitLocalCommand no
  ConnectTimeout 5
''')
        try:
            result = subprocess.run(['/usr/bin/ssh', '-F', str(config), row['alias'], 'whoami'],
                                    capture_output=True, check=True, timeout=8, text=True)
            if result.stdout.strip().split('\\')[-1].casefold() != username.casefold():
                raise ValueError()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise DeviceError('The private SSH connection could not be verified') from None
        with closing(self.connect()) as db, db:
            changed = db.execute('UPDATE devices SET ready=1,username=?,hostkey=? WHERE id=? AND proof=? AND expires>?',
                                 (username, key, row['id'], row['proof'], int(time.time()))).rowcount
        if not changed:
            raise DeviceError('Device pairing expired before verification')
        return self.command(row)

    def command(self, row):
        return shlex.join(['ssh', '-F', str(self.ssh_folder(row['id'])/'config'), row['alias']])

    def renew(self, row):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE devices SET expires=? WHERE id=? AND proof=?',
                       (int(time.time())+LEASE_SECONDS, row['id'], row['proof']))

    def revoke(self, row):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('DELETE FROM devices WHERE id=? AND proof=?', (row['id'], row['proof'])).rowcount:
                self.remove_identity(row['id'])

    def remove_identity(self, device):
        parent = self.ssh_folder(device)
        if not parent.exists():
            return
        for name in ('config', 'known_hosts', 'identity', 'identity.pub'):
            (parent/name).unlink(missing_ok=True)
        parent.rmdir()

    def permit_frp(self, request):
        op = request.get('op')
        payload = request.get('content')
        if not isinstance(payload, dict):
            return False
        user = payload if op == 'Login' else payload.get('user')
        if not isinstance(user, dict):
            return False
        metadata = user.get('metas')
        if not isinstance(metadata, dict):
            return False
        device, proof = metadata.get('device'), metadata.get('proof')
        if not isinstance(device, str) or not DEVICE.fullmatch(device):
            return False
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM devices WHERE id=? AND expires>?',
                             (device, int(time.time()))).fetchone()
        if row is None or not self.permitted(row['owner']) or not self.proof_matches(row, proof):
            return False
        if op == 'NewProxy':
            return (payload.get('proxy_type') == 'tcp' and payload.get('proxy_name') == row['alias']
                    and payload.get('remote_port') == row['port'] and not payload.get('group'))
        if op == 'NewUserConn':
            return payload.get('proxy_type') == 'tcp' and payload.get('proxy_name') == row['alias']
        return op in ('Login', 'Ping', 'NewWorkConn')

    def chat_binding(self, owner, platform, device, proof):
        row = self.enrolled(owner, platform, device, proof)
        if not row['ready'] or row['expires'] <= time.time():
            raise DeviceError('SafeCode device control is offline')
        try:
            with socket.create_connection(('127.0.0.1', row['port']), timeout=2) as connection:
                if not connection.recv(64).startswith(b'SSH-'):
                    raise OSError()
        except OSError:
            raise DeviceError('SafeCode device control is offline') from None
        return {'id': row['id'], 'platform': row['platform'], 'hostname': row['hostname'],
                'alias': row['alias'], 'command': self.command(row)}


def read_json(handler):
    length = int(handler.headers.get('Content-Length', '0'))
    if handler.headers.get('Transfer-Encoding') or not 0 < length <= MAX_JSON:
        raise DeviceError('Invalid device request')
    body = json.loads(handler.rfile.read(length))
    if not isinstance(body, dict):
        raise DeviceError('Invalid device request')
    return body


class FrpAuthorization(BaseHTTPRequestHandler):
    timeout = 3

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            valid = (self.path.partition('?')[0] == self.server.callback
                     and self.server.devices.permit_frp(read_json(self)))
        except (ValueError, OSError, sqlite3.Error):
            valid = False
        response = json.dumps({'reject': not valid, 'unchange': True,
                               'reject_reason': '' if valid else 'Device authorization required'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--callback-file', type=Path, required=True)
    args = parser.parse_args()
    callback = args.callback_file.read_text().strip()
    if not re.fullmatch(r'/[a-f0-9]{48}', callback):
        parser.error('Invalid callback path')
    with ThreadingHTTPServer(('127.0.0.1', args.port), FrpAuthorization) as server:
        server.callback = callback
        server.devices = Devices(args.state)
        server.serve_forever()
