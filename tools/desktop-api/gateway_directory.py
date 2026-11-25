"""Client for registered gateways. The authorization service owns grants and code redemption."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

AUTHORITY = 'safecode.tommukan.dpdns.org'
DIRECTORY = {
    'multi.list': ('/v1/list', ()),
    'multi.activate': ('/v1/activate', ('code', 'name', 'address')),
    'multi.inspect': ('/v1/pair/inspect', ('code',)),
    'multi.create': ('/v1/pair/create', ('review',)),
    'multi.claim': ('/v1/pair/claim', ('code',)),
    'multi.remove': ('/v1/pair/remove', ('gateway',)),
}
# Only these operations can cross a grant. Never forward another target or a host operation.
REMOTE_FIELDS = {
    'list': {'details', 'analytics', 'background'}, 'usage': {'id', 'background'},
    'model_catalog': set(), 'refresh_subscription': set(),
    'issue': {'note', 'rpm', 'budget_cents', 'allowed_models', 'expires_at', 'activation_code'},
    'update': {'id', 'note', 'rpm', 'budget_cents', 'allowed_models', 'expires_at'},
    'revoke': {'id'}, 'delete': {'id'}, 'set_concurrency': {'concurrency'},
    'set_key_queue': {'enabled'},
    'set_prompt_review': {'enabled', 'provider', 'model', 'deepseek_key', 'language_replacement'},
}


class DirectoryError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check_operation(request):
    fields = REMOTE_FIELDS.get(request.get('action'))
    if fields is None or request.keys() - fields - {'action'}:
        raise DirectoryError(400, '该操作不能通过已绑定网关执行。')


def saved_identity(folder):
    from cryptography.hazmat.primitives import serialization as fmt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / 'identity.json'
    if not path.exists():
        def raw(key):
            return key.private_bytes(fmt.Encoding.Raw, fmt.PrivateFormat.Raw, fmt.NoEncryption()).hex()
        identity = {'id': secrets.token_hex(16), 'sign': raw(Ed25519PrivateKey.generate()),
                    'exchange': raw(X25519PrivateKey.generate())}
        with tempfile.NamedTemporaryFile(mode='w', dir=folder) as staged:
            json.dump(identity, staged)
            staged.flush()
            os.fsync(staged.fileno())
            try:
                os.link(staged.name, path)
            except FileExistsError:
                pass
    identity = json.loads(path.read_text())
    return (identity['id'], Ed25519PrivateKey.from_private_bytes(bytes.fromhex(identity['sign'])),
            X25519PrivateKey.from_private_bytes(bytes.fromhex(identity['exchange'])))


def public_bytes(key, der=False):
    from cryptography.hazmat.primitives import serialization as fmt
    return key.public_key().public_bytes(fmt.Encoding.DER if der else fmt.Encoding.Raw,
                                        fmt.PublicFormat.SubjectPublicKeyInfo if der else fmt.PublicFormat.Raw)


def envelope(key, peer_bytes, context):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    shared = key.exchange(X25519PublicKey.from_public_bytes(peer_bytes))
    return AESGCM(HKDF(hashes.SHA256(), 32, None, context).derive(shared))


def encode(box, aad, value, prefix=b''):
    nonce = os.urandom(12)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    return base64.b64encode(prefix + nonce + box.encrypt(nonce, raw, aad)).decode()


def decode(box, aad, data):
    return json.loads(box.decrypt(data[:12], data[12:], aad))


class GatewayDirectory:
    def __init__(self, root, manage, enabled):
        self.root = root
        self.folder = root / 'gateway-directory'
        self.manage = manage
        self.enabled = enabled
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.identity = None
        self.worker = None
        if (self.folder / 'registered').is_file():
            self.start()

    def keys(self):
        with self.lock:
            if self.identity is None:
                self.identity = saved_identity(self.folder)
            return self.identity

    def post(self, path, payload):
        device, key, _ = self.keys()
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        signed = '\n'.join(('SC-GATEWAY-1', 'POST', path, device, timestamp, nonce,
                            hashlib.sha256(raw).hexdigest())).encode()
        conn = http.client.HTTPSConnection(AUTHORITY, timeout=5)
        try:
            conn.request('POST', '/gateway-authority' + path, raw, {
                'Content-Type': 'application/json', 'X-Gateway': device, 'X-Time': timestamp,
                'X-Nonce': nonce, 'X-Signature': base64.b64encode(key.sign(signed)).decode()})
            reply = conn.getresponse()
            limit = 8 * 1024 * 1024 if path == '/v1/manage/wait' else 256 * 1024
            body = reply.read(limit + 1)
            if len(body) > limit:
                raise ValueError()
            value = json.loads(body)
            if reply.status != 200:
                raise DirectoryError(reply.status, value.get('error') or '授权服务拒绝了请求。')
            return value
        except DirectoryError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise DirectoryError(503, '连接授权服务失败，请稍后重试。') from error
        finally:
            conn.close()

    def request(self, request):
        action = request['action']
        if 'gateway_target' in request:
            target = request['gateway_target']
            if not isinstance(target, str) or not re.fullmatch('[a-f0-9]{32}', target):
                raise DirectoryError(400, '网关标识无效。')
            options = {k:v for k,v in request.items() if k != 'gateway_target'}
            check_operation(options)
            return self.remote(target, options)
        path, fields = DIRECTORY[action]
        if set(request) != set(fields) | {'action'}:
            raise DirectoryError(400, '绑定参数不正确。')
        if action == 'multi.list' and not (self.folder / 'identity.json').exists():
            raise DirectoryError(401, '请先登记本机网关。')
        data = {field: request[field] for field in fields}
        if action == 'multi.activate':
            data['public_key'] = base64.b64encode(public_bytes(self.keys()[1], der=True)).decode()
        value = self.post(path, data)
        if action in ('multi.activate', 'multi.list'):
            (self.folder / 'registered').touch(mode=0o600, exist_ok=True)
            self.start()
        return {'value': value}

    def remote(self, target, request):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        public = self.post('/v1/manage/target', {'gateway': target})['public']
        job = secrets.token_hex(16)
        context = f'SafeCode admin v1 {job} {self.keys()[0]} {target}'.encode()
        key = X25519PrivateKey.generate()
        box = envelope(key, base64.b64decode(public, validate=True), context)
        sealed = encode(box, context + b' request', request, public_bytes(key))
        uncertain = '未能确认对方网关的执行结果。请刷新后再决定是否重试。'
        try:
            self.post('/v1/manage/submit', {'gateway': target, 'id': job, 'sealed': sealed})
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                reply = self.post('/v1/manage/wait', {'id': job})['sealed']
                if reply:
                    return decode(box, context + b' response', base64.b64decode(reply, validate=True))
                self.stop.wait(.5)
        except DirectoryError as error:
            if error.status < 500:
                raise
        raise DirectoryError(504, uncertain)

    def handle_job(self, job):
        device, _, key = self.keys()
        if not all(isinstance(job.get(k), str) and re.fullmatch('[a-f0-9]{32}', job[k]) for k in ('id', 'parent')):
            return
        context = f'SafeCode admin v1 {job["id"]} {job["parent"]} {device}'.encode()
        data = base64.b64decode(job['sealed'], validate=True)
        box = envelope(key, data[:32], context)
        request = decode(box, context + b' request', data[32:])
        try:
            check_operation(request)
            if not self.enabled():
                raise ValueError('网关已停用。')
            value = {'value': self.manage(request)}
        except (KeyError, TypeError, ValueError, OSError) as error:
            value = {'error': str(error)}
        self.post('/v1/manage/result', {'id': job['id'], 'sealed': encode(box, context + b' response', value)})

    def snapshot(self):
        report = self.manage({'action':'list', 'details':True})
        keys = report.get('keys', [])
        billing = report.get('billing') or {}
        return {'requests':sum(k.get('requests') or 0 for k in keys),
                'tokens':sum(k.get('tokens') or 0 for k in keys),
                'cost_usd':(billing.get('cost_nanos') or 0) / 1e9,
                'running':report.get('active'), 'queued':report.get('waiting'),
                'quota':{name:{k:v for k,v in window.items() if k in ('used','resets_at')}
                        for name,window in (report.get('subscription') or {}).get('windows', {}).items()
                        if name in ('five_hour','seven_day') and isinstance(window, dict)}}

    def start(self):
        with self.lock:
            if self.worker is None:
                self.worker = threading.Thread(target=self.sync, name='gateway-directory', daemon=True)
                self.worker.start()

    def sync(self):
        from cryptography.exceptions import InvalidTag
        announce = 0
        while not self.stop.is_set():
            delay = 1
            try:
                if self.enabled():
                    if time.monotonic() >= announce:
                        public = base64.b64encode(public_bytes(self.keys()[2])).decode()
                        self.post('/v1/manage/announce', {'public': public})
                        self.post('/v1/heartbeat', {'snapshot': self.snapshot()})
                        announce = time.monotonic() + 30
                    job = self.post('/v1/manage/pull', {})['job']
                    if job:
                        self.handle_job(job)
                else:
                    delay = 5
            except (ValueError, KeyError, TypeError, OSError, InvalidTag):
                delay = 5
            self.stop.wait(delay)


def installed_directory(root, request):
    # Earlier registered installations keep their original identity and worker.
    state = Path('/var/lib/safe-code-mg-connector')
    script = Path('/opt/safe-code-mg-connector/connector.py')
    if not (state / 'identity.json').is_file() or not script.is_file():
        return None
    program = """import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from connector import Connector, RemoteError
client = Connector(Path(sys.argv[2]))
if client.source.resolve() != Path(sys.argv[3]).resolve():
    raise ValueError('Gateway identity is associated with another instance.')
request = json.load(sys.stdin)
try:
    target = request.pop('gateway_target', None)
    result = client.manage(target, request) if target else {'value': client.browser(request)}
except RemoteError as error:
    result = {'error': str(error), 'status': error.status}
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(script), str(state),
                             str(root / 'admin.sock')], input=json.dumps(request), capture_output=True,
                            timeout=24, text=True)
    if result.returncode:
        raise DirectoryError(503, '现有网关登记暂不可用，请检查连接器状态。')
    return json.loads(result.stdout)
