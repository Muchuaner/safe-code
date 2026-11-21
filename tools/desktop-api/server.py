"""Standalone TLS Messages API for the SafeCode desktop bridge, independent of SSH leases."""
import argparse
import base64
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import socketserver
import sqlite3
import signal
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from claude_usage import read_subscription
from desktop_gateway import CLOUDCLI_REQUIRED, COUNT_TOKENS_PATH, MAX_REQUEST, MESSAGES_PATH, GatewayError, Handler, Manager, diagnostic, load_cloudcli_config
from device_bridge import DeviceError, Devices, OwnerHosts, SSH_DEVICE, read_json
from management import ANALYTICS_RANGES, KEY_ID, TOKEN_FIELDS, Capacity, KeyRegistry, RequestQueue, peer_disconnected, read_admin_line, send_admin

PUBLIC_ENABLED = 'public-enabled'
PUBLIC_ORIGIN = 'public-origin'
PUBLIC_CONCURRENCY = 'public-concurrency'
PUBLIC_DISABLED_MESSAGE = '请先在 Safe Code 中确认「保证自用」并启用公网。'
# A shared promotional code, checked only at the gateway.
ACTIVATION_CODE_SHA256 = bytes.fromhex('89debb851e3fc69c8e163a3b6462a3493e93d38ae8cf0132e419bd9e0b32d2b9')
# Shared with DesktopKeyAccess. These bundled keys deter casual inspection only.
TICKET_ENCRYPTION_KEY = 'f04223626f9a64f32aa618705067a6a04a5d852f8e5b551c944d0b9cd2d7f54d'
TICKET_AUTH_KEY = bytes.fromhex('bf8145e375d11db0fdd2fb0d9734fdda504c5a480fc2eae4a567c1948aa86155')


class PublicManager(Manager):
    def __init__(self, folder, config, *, require_cloudcli=True):
        super().__init__(folder, config, require_cloudcli=require_cloudcli)
        self.lock = threading.RLock()
        self.accounting = threading.local()

    def capture(self, turn_id):
        touched = getattr(self.accounting,'before',None)
        if touched is not None and turn_id not in touched:
            touched[turn_id] = self.turn_usage(turn_id)

    def turn_usage(self, turn_id):
        result = self.completed_usage.get(turn_id)
        if result is not None:
            return result.copy()
        parts = [self.usage[key] for key in self.turn_messages.get(turn_id, ())]
        return {key:sum(part.get(key,0) for part in parts) for key in (*TOKEN_FIELDS, 'one_hour_cache_writes')}

    @staticmethod
    def one_hour_writes(usage):
        cache = usage.get('cache_creation')
        count = cache.get('ephemeral_1h_input_tokens') if isinstance(cache, dict) else None
        return count if type(count) is int and count >= 0 else None

    def count_usage(self, message, turn_id='unbound'):
        with self.lock:
            if isinstance(message.get('model'), str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}(?:\[1m\])?', message['model']):
                self.accounting.model = message['model']
            self.capture(turn_id)
            super().count_usage(message,turn_id)
            identity = message.get('id')
            count = self.one_hour_writes(message.get('usage', {}))
            if identity and count is not None:
                previous = self.usage[identity]
                previous['one_hour_cache_writes'] = max(previous.get('one_hour_cache_writes', 0), count)

    def record_result(self, turn_id, usage):
        if not isinstance(usage, dict) or not usage:
            return
        with self.lock:
            self.capture(turn_id)
            super().record_result(turn_id,usage)
            count = self.one_hour_writes(usage)
            if count is None:
                count = sum(self.usage[key].get('one_hour_cache_writes', 0)
                            for key in self.turn_messages.get(turn_id, ()))
            self.completed_usage[turn_id]['one_hour_cache_writes'] = count

    def take_usage(self):
        with self.lock:
            totals = dict.fromkeys((*TOKEN_FIELDS, 'one_hour_cache_writes'), 0)
            pending_turns = {chat.turn_id for chat in self.conversations if chat.pending}
            for turn_id, before in self.accounting.before.items():
                after = self.turn_usage(turn_id)
                for field in totals:
                    totals[field] += after.get(field, 0) - before.get(field, 0)
                if turn_id not in pending_turns:
                    self.clear_turn_usage(turn_id)
            self.accounting.before.clear()
            return totals

    def clear_turn_usage(self, turn_id):
        self.completed_usage.pop(turn_id, None)
        for identity in self.turn_messages.pop(turn_id, ()):
            self.usage.pop(identity, None)

    def finish_conversation_request(self, conversation):
        if hasattr(self.accounting, 'before'):
            self.accounting.finished = self.take_usage()
            if not conversation.pending:
                self.clear_turn_usage(conversation.turn_id)


class AdminHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(6)
        try:
            result={'value':self.server.manage(read_admin_line(self.rfile))}
        except (ValueError, KeyError, TypeError) as error:
            result={'error':str(error)}
        except (OSError, sqlite3.Error):
            result={'error':'Safe Code API 无法读取或保存本机管理数据。'}
        self.wfile.write(json.dumps(result,ensure_ascii=False).encode()+b'\n')



def relay_frp(client, upstream):
    # The client starts the FRP protocol only after the CONNECT response.
    # Read decrypted bytes already buffered by TLS before waiting on the socket.
    while True:
        ready = [client] if client.pending() else []
        readable, _, _ = select.select([client, upstream], [], [], 0 if ready else 90)
        if not (ready or readable):
            return
        for source in set(ready + readable):
            data = source.recv(65536)
            if not data:
                return
            destination = upstream if source is client else client
            destination.sendall(data)


class PublicHandler(Handler):
    timeout = 12

    def setup(self):
        self.request.settimeout(self.timeout)
        super().setup()

    @property
    def manager(self):
        return self.server.account(self.digest, self.platform, self.device)

    def authorized(self):
        value = self.headers.get('Authorization', '')
        token = value[7:] if value.startswith('Bearer ') else self.headers.get('x-api-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not self.server.registry.enabled(digest):
            return False
        platform = self.headers.get('X-SafeCode-Platform', 'windows')
        if platform not in ('windows', 'macos'):
            return False
        self.digest, self.platform = digest, platform
        return True

    def do_GET(self):
        self.device = None
        if self.path.partition('?')[0] == '/v1/cloudcli/models':
            self.path = self.path.replace('/v1/cloudcli/', '/v1/', 1)
        if self.path.partition('?')[0] != '/v1/models':
            return self.reply(404, {'error':{'message':'Unknown endpoint'}})
        if not self.server.public_enabled:
            return self.public_disabled()
        if self.server.config['cloudcli'] is None:
            if not self.authorized():
                return self.device_error(401, 'SafeCode Key required')
            return self.backend_required()
        super().do_GET()

    def do_CONNECT(self):
        self.close_connection = True
        if self.path != '/v1/device-frp':
            return self.device_error(404, 'Unknown endpoint')
        if not self.server.public_enabled or not self.authorized() or not self.server.devices:
            return self.device_error(403, 'SafeCode device authorization is unavailable')
        try:
            row = self.server.devices.enrolled(
                self.digest, self.platform, self.headers.get('X-SafeCode-Device'),
                self.headers.get('X-SafeCode-Device-Proof'))
            if row['expires'] <= time.time():
                raise DeviceError('Device lease expired')
        except (DeviceError, sqlite3.Error):
            return self.device_error(403, 'SafeCode device authorization is unavailable')
        # The target is fixed by the gateway operator. This is not an HTTP proxy.
        try:
            upstream = socket.create_connection(('127.0.0.1', self.server.frp_port), timeout=4)
        except OSError:
            return self.device_error(503, 'SafeCode FRP is unavailable')
        with upstream:
            try:
                self.send_response(200)
                self.end_headers()
                upstream.settimeout(10)
                self.connection.settimeout(10)
                relay_frp(self.connection, upstream)
            except OSError:
                pass

    def do_POST(self):
        self.device = None
        if self.path.partition('?')[0] in ('/v1/cloudcli/messages', '/v1/cloudcli/messages/count_tokens'):
            self.path = self.path.replace('/v1/cloudcli/', '/v1/', 1)
        if self.path.partition('?')[0] not in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            return self.handle_public()
        with self.request_diagnostic('unknown'):
            self.handle_public()

    def handle_public(self):
        path = self.path.partition('?')[0]
        if path not in (MESSAGES_PATH, COUNT_TOKENS_PATH, '/v1/device-control'):
            return self.reply(404, {'error':{'message':'Unknown endpoint'}})
        if not self.server.public_enabled:
            return self.public_disabled()
        if path == '/v1/device-control':
            return self.device_control()
        device = self.headers.get('X-SafeCode-Device')
        proof = self.headers.get('X-SafeCode-Device-Proof')
        if not self.authorized():
            return self.reject_device_message(401, 'SafeCode Key required')
        if self.server.config['cloudcli'] is None:
            return self.reject_device_message(503, CLOUDCLI_REQUIRED)
        if self.headers.get('X-SafeCode-Diagnostics') == '1':
            active, waiting, slots = self.server.queue.snapshot()
            diagnostic(backend='cloudcli' if self.server.config['cloudcli'] else 'claude_code',
                       active=min(1000000, sum(active.values())), waiting=min(1000000, sum(waiting.values())),
                       slots=min(1000000, slots))
        if device is None and proof is None:
            if (self.server.root/'device-required'/self.digest).is_file():
                return self.reject_device_message(403, 'Verify SafeCode local device control before starting a chat')
        else:
            diagnostic(stage='device_check')
            try:
                binding = self.server.owner_hosts if SSH_DEVICE.fullmatch(device or '') else self.server.devices
                if binding is None:
                    raise DeviceError('Device pairing is unavailable')
                self.device = binding.chat_binding(self.digest, self.platform, device, proof)
            except (DeviceError, sqlite3.Error):
                return self.reject_device_message(403, 'SafeCode device authorization is unavailable')
        super().handle_messages()

    def reject_device_message(self, status, message):
        # Read a bounded request before closing TLS so the rejection body reaches the relay.
        try:
            remaining = int(self.headers.get('Content-Length', '0'))
            if not self.headers.get('Transfer-Encoding') and 0 < remaining <= MAX_REQUEST:
                while remaining:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
        except (ValueError, OSError):
            pass
        return self.device_error(status, message)

    def device_error(self, status, message):
        return self.reply(status, {'type': 'error', 'error': {'type': 'permission_error', 'message': message}})

    def device_control(self):
        if not self.authorized():
            return self.device_error(401, 'SafeCode Key required')
        try:
            request = read_json(self)
            action = request.get('action')
            device = request.get('deviceId')
            if action == 'revoke' and isinstance(device, str) and SSH_DEVICE.fullmatch(device):
                self.server.owner_hosts.revoke(self.digest, self.platform, device, request.get('deviceProof'))
                return self.reply(200, {'revoked':True})
            devices = self.server.devices
            if devices is None or not devices.permitted(self.digest):
                return self.device_error(403, 'Device control is unavailable for this Key')
            if action == 'capability':
                return self.reply(200, {'supported': True, 'transport': 'frp', 'httpsTunnel': True, 'port': self.server.frp_port,
                                        'deviceRequired': devices.required(self.digest)})
            if action == 'prepare':
                if request.get('accepted') is not True:
                    raise DeviceError('Local control consent is required')
                return self.reply(200, devices.prepare(self.digest, self.platform, request.get('hostname')))
            row = devices.enrolled(self.digest, self.platform, request.get('deviceId'), request.get('deviceProof'))
            if action == 'verify':
                return self.reply(200, {'command': devices.configure(row, request.get('username'), request.get('hostKey'))})
            if action == 'renew':
                devices.renew(row)
                return self.reply(200, {'ready': bool(row['ready'])})
            if action == 'revoke':
                devices.revoke(row)
                return self.reply(200, {'revoked': True})
            raise DeviceError('Unknown device action')
        except (DeviceError, ValueError, TypeError):
            return self.device_error(400, 'SafeCode could not apply the device request')
        except (OSError, sqlite3.Error):
            return self.device_error(503, 'SafeCode device control is unavailable')

    def public_disabled(self):
        self.close_connection = True
        return self.reply(403, {'error': {'type': 'permission_error', 'message': PUBLIC_DISABLED_MESSAGE}})

    def backend_required(self):
        return self.reply(503, {'type': 'error', 'error': {'type': 'api_error', 'message': CLOUDCLI_REQUIRED}})

    def execute_message(self, data, **options):
        if not isinstance(data,dict):
            raise GatewayError('Invalid Messages request',400)
        requested = data.get('model')
        model = requested if isinstance(requested, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}(?:\[1m\])?', requested) else 'unknown'
        enqueued = time.monotonic()
        started = None
        record = {'model': model, 'outcome': 'success', 'status': 200, 'wait_ms': 0, 'duration_ms': None}
        diagnostic(stage='gateway_queue')
        try:
            with self.server.queue.slot(self.digest, lambda: peer_disconnected(self.connection)):
                started = time.monotonic()
                diagnostic(stage='gateway_validation', waitMs=round((started-enqueued)*1000))
                manager = self.manager
                manager.accounting.before = {}
                manager.accounting.model = model
                manager.accounting.finished = None
                try:
                    return manager.run(data, **options)
                except GatewayError as error:
                    record.update(outcome='failed', status=error.status)
                    raise
                except ConnectionAbortedError:
                    record.update(outcome='canceled', status=None)
                    raise
                except Exception:
                    record.update(outcome='failed', status=None)
                    raise
                finally:
                    usage = manager.accounting.finished
                    if usage is None:
                        usage = manager.take_usage()
                    del manager.accounting.before
                    record['wait_ms'] = round((started - enqueued) * 1000)
                    record['duration_ms'] = round((time.monotonic() - started) * 1000)
                    record['model'] = getattr(manager.accounting, 'model', model)
                    self.server.registry.record(self.digest, record['model'],
                                                record['outcome'] != 'success', usage, request=record)
        except GatewayError as error:
            if started is None:
                record.update(status=error.status, outcome='limited' if error.status in (402, 429) else 'canceled')
            raise
        except ConnectionAbortedError:
            record.update(outcome='canceled', status=None)
            raise
        except Exception:
            record.update(outcome='failed', status=None)
            raise
        finally:
            if started is None and record['outcome'] != 'success':
                record['wait_ms'] = round((time.monotonic() - enqueued) * 1000)
                try:
                    self.server.registry.record_unadmitted(self.digest, record)
                except sqlite3.Error:
                    self.log_error('Safe Code API could not record request outcome')


class PublicServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, config):
        cloudcli = load_cloudcli_config()
        if cloudcli and not ipaddress.ip_address(config['bind']).is_loopback and (
                cloudcli.get('hostControl') or
                cloudcli.get('desktopQuestions') and not cloudcli.get('desktopToolApprovals')):
            raise ValueError('Direct CloudCLI gateways cannot use a shared workstation or disable configured Desktop tool approvals.')
        self.root, self.config = root, {**config, 'cloudcli': cloudcli}
        self.frp_port = config.get('deviceFrpPort')
        if self.frp_port is not None and (type(self.frp_port) is not int or not 1024 <= self.frp_port <= 65535):
            raise ValueError('Invalid private FRP control port')
        self.devices = Devices(root) if cloudcli and self.frp_port else None
        self.owner_hosts = OwnerHosts(root)
        self.accounts = {}
        self.lock = threading.Lock()
        self.usage_lock = threading.Lock()
        self.subscription = {'state': 'unavailable', 'windows': {}}
        self.registry = KeyRegistry(root)
        self.registry.prune_requests()
        self.next_log_cleanup = time.monotonic() + 3600
        try:
            concurrency = int((root/PUBLIC_CONCURRENCY).read_text())
        except (FileNotFoundError, ValueError):
            concurrency = 0
        self.queue = RequestQueue(self.registry, concurrency=max(0, concurrency))
        # CLI turns can retain their processes between HTTP requests while awaiting tools.
        self.turns = Capacity(lambda: self.queue.concurrency + 2 if self.queue.concurrency else 0)
        self.webfetch_turns = Capacity(lambda: self.queue.concurrency)
        self.workers = Capacity(lambda: max(24, self.queue.concurrency + self.queue.max_queue + 8))
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.load_cert_chain(root/'cert.pem', root/'key.pem')
        super().__init__((config['bind'], config['port']), PublicHandler)

    def process_request(self, request, address):
        if not self.workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(10)
            with self.tls.wrap_socket(request, server_side=True) as connection:
                self.finish_request(connection, address)
        except (OSError, TimeoutError):
            pass
        finally:
            request.close()
            self.workers.release()

    def service_actions(self):
        # A closed Desktop can leave a CLI waiting for a tool result indefinitely.
        now = time.monotonic()
        if now >= self.next_log_cleanup:
            self.next_log_cleanup = now + 3600
            try:
                self.registry.prune_requests()
            except sqlite3.Error:
                pass
        if now < getattr(self, 'next_cleanup', 0):
            return
        self.next_cleanup = now + 30
        with self.lock:
            managers = list(self.accounts.values())
        abandoned = []
        for manager in managers:
            with manager.lock:
                for chat in list(manager.conversations):
                    if not chat.handling and now - chat.last_used > 600:
                        manager.conversations.remove(chat)
                        manager.clear_turn_usage(chat.turn_id)
                        abandoned.append(chat)
        for chat in abandoned:
            chat.close()

    def account(self, digest, platform, device=None):
        identity = digest+'-'+platform+('-'+device['id'] if device else '')
        with self.lock:
            if identity not in self.accounts:
                folder = self.root/'sessions'/identity
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                m = PublicManager(folder, {**self.config, 'instance':digest[:32], 'clientPlatform':platform,
                                           'pairedDevice':device})
                if m.cloudcli:
                    m.cloudcli.parallel = True
                m.live_slots = self.turns
                m.webfetch_slots = self.webfetch_turns
                self.accounts[identity] = m
            return self.accounts[identity]

    def manage(self, request):
        operation = request.get('action')
        if operation == 'public_status':
            status = {'enabled': self.public_enabled, 'backend': 'cloudcli' if self.config['cloudcli'] else 'unconfigured'}
            if status['enabled'] and request.get('runtime') is True:
                active, waiters, concurrency = self.queue.snapshot()
                status.update(active=sum(active.values()), waiting=sum(waiters.values()),
                              concurrency=concurrency, concurrency_text=str(concurrency), queue_limit=self.queue.max_queue,
                              subscription=self.subscription)
            return status
        if operation == 'enable_public':
            if request.get('accepted') is not True:
                raise ValueError(PUBLIC_DISABLED_MESSAGE)
            if self.config['cloudcli'] is None:
                raise ValueError(CLOUDCLI_REQUIRED)
            (self.root/PUBLIC_ENABLED).touch(mode=0o600, exist_ok=True)
            return {'enabled': self.public_enabled, 'backend': 'cloudcli'}
        if not self.public_enabled:
            raise ValueError(PUBLIC_DISABLED_MESSAGE)
        if operation == 'refresh_subscription':
            if not self.usage_lock.acquire(blocking=False):
                return {'state': 'loading', 'windows': self.subscription['windows']}
            try:
                try:
                    windows = read_subscription(timeout=6)
                except (OSError, ValueError, AttributeError, TypeError):
                    windows = {}
                if windows:
                    self.subscription = {'state': 'available', 'windows': windows, 'sampled_at': int(time.time())}
                else:
                    self.subscription = {**self.subscription, 'state': 'stale' if self.subscription['windows'] else 'unavailable'}
                return self.subscription
            finally:
                self.usage_lock.release()
        if operation == 'set_concurrency':
            concurrency = request.get('concurrency')
            try:
                if isinstance(concurrency, str) and re.fullmatch(r'0|[1-9][0-9]*', concurrency):
                    concurrency = int(concurrency)
                if type(concurrency) is not int or concurrency < 0:
                    raise ValueError()
            except ValueError:
                raise ValueError('处理槽位必须是非负整数，0 表示不限。') from None
            with self.lock:
                atomic_write(self.root/PUBLIC_CONCURRENCY, str(concurrency))
                self.queue.resize(concurrency)
            return {'concurrency': concurrency, 'concurrency_text': str(concurrency)}
        if operation == 'set_origin':
            origin = validate_origin(request.get('origin'))
            atomic_write(self.root/PUBLIC_ORIGIN, origin)
            return self.manage({'action': 'settings'})
        if operation == 'settings':
            bind, port = self.server_address[:2]
            address = ipaddress.ip_address(bind)
            public_ip = self.config.get('publicIp')
            if not public_ip and address.is_global and not address.is_multicast:
                public_ip = bind
            active, waiting, _ = self.queue.snapshot()
            return {'bind': bind, 'port': port, 'publicIp': public_ip, 'origin': self.public_origin,
                    'busy': bool(active or waiting)}
        if operation=='list':
            active, waiters, concurrency = self.queue.snapshot()
            keys = self.registry.list()
            for key in keys:
                key['active_count'] = active.get(key['id'], 0)
                key['active'] = bool(key['active_count'])
                key['waiting'] = waiters.get(key['id'],0)
            result = {'keys':keys,'origin':self.public_origin,
                      'activation_required':any(key['enabled'] for key in keys),
                      'can_delete_revoked_keys': True,
                      'billing': {'cost_nanos': sum(key['cost_nanos'] for key in keys),
                                  'unpriced_tokens': sum(key['unpriced_tokens'] for key in keys)},
                      'active':bool(active),'active_count':sum(active.values()),'waiting':sum(waiters.values()),
                      'concurrency':concurrency,'concurrency_text':str(concurrency),'queue_limit':self.queue.max_queue,
                      'wait_limit':self.queue.max_wait}
            if request.get('details') is True:
                result.update(self.registry.overview(), recent=self.registry.recent_requests())
                result['subscription'] = self.subscription
                period = request.get('analytics')
                if isinstance(period, str) and period in ANALYTICS_RANGES:
                    result['analytics'] = self.registry.analytics(period)
            return result
        if operation in ('issue','update'):
            note, rpm = request.get('note',''), request.get('rpm',0)
            if not isinstance(note,str) or len(note)>120 or type(rpm) is not int or not 0<=rpm<=600:
                raise ValueError('备注限 120 字，请求频率应为 0 到 600 次/分。')
            budget = request.get('budget_cents')
            if budget is not None and (type(budget) is not int or not 0 <= budget <= 100_000_000):
                raise ValueError('累计额度应为 0 到 1,000,000 美元，最多两位小数。')
            billing = {'budget_nanos': None if budget is None else budget * 10_000_000} if 'budget_cents' in request else {}
            if operation=='issue':
                if self.config['cloudcli'] is None:
                    raise ValueError(CLOUDCLI_REQUIRED)
                origin = self.public_origin or request.get('origin')
                if not origin:
                    raise ValueError('无法识别 VPS 的公网 IP，请从 Safe Code 的 SSH 会话创建密钥。')
                ticket, key_id = make_key(self.root,origin)
                self.registry.issue(key_id, note.strip(), rpm, activated=valid_activation_code(request.get('activation_code')), **billing)
                return {'id':key_id,'ticket':ticket}
        key_id = request.get('id','')
        if not isinstance(key_id,str) or not KEY_ID.fullmatch(key_id):
            raise ValueError('Key ID 无效。')
        if operation=='usage':
            return {'models':self.registry.usage(key_id), 'recent':self.registry.recent_requests(key_id)}
        if operation=='update':
            self.registry.update(key_id,note.strip(),rpm,**billing)
        elif operation=='revoke':
            self.registry.revoke(key_id)
        elif operation == 'delete':
            self.registry.delete(key_id)
        else:
            raise ValueError('未知 Safe Code API 管理操作。')
        self.queue.wake()
        return {'id':key_id}

    @property
    def public_enabled(self):
        return (self.root/PUBLIC_ENABLED).is_file()

    @property
    def public_origin(self):
        try:
            return (self.root/PUBLIC_ORIGIN).read_text()
        except FileNotFoundError:
            return self.config.get('publicOrigin', '')

def open_public_server(root, config):
    port = config['port']
    automatic = config.get('autoPort', 45143) == port and not ipaddress.ip_address(config['bind']).is_loopback
    automatic = automatic and not config.get('publicOrigin') and not (root/PUBLIC_ORIGIN).exists()
    candidates = [*range(port, min(port + 10, 65535) + 1), 0] if automatic else [port]
    for candidate in candidates:
        try:
            server = PublicServer(root, {**config, 'port': candidate})
        except OSError as error:
            if error.errno != errno.EADDRINUSE or candidate == candidates[-1]:
                raise
            continue
        actual_port = server.server_port
        if actual_port != port:
            try:
                atomic_write(root/'config.json', json.dumps({**config, 'port': actual_port, 'autoPort': actual_port}))
            except BaseException:
                server.server_close()
                raise
        return server


def configure_port(root, expected_port, port, *, check_available=True):
    if not 1024 <= port <= 65535:
        raise ValueError('监听端口必须是 1024 到 65535 之间的整数。')
    path = root/'config.json'
    config = json.loads(path.read_text())
    if config['port'] != expected_port:
        raise ValueError('监听端口已变更，请重新读取网关状态。')
    if check_available:
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((config['bind'], port))
        except OSError as error:
            raise ValueError('新端口无法监听，可能已被其他服务占用。') from error
    config['port'] = port
    atomic_write(path, json.dumps(config))


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temp:
        staged = Path(temp.name)
        try:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def validate_origin(origin):
    try:
        if not isinstance(origin, str) or len(origin) > 400 or not re.fullmatch(r'https://[^\s/\\?#@]+/?', origin):
            raise ValueError()
        url = urlsplit(origin)
        if not url.hostname or url.port == 0 or url.netloc.endswith(':'):
            raise ValueError()
    except ValueError:
        raise ValueError('请输入 HTTPS 公网地址及端口，地址中不能包含路径或凭据。')
    return origin.rstrip('/')


def make_key(root, origin):
    origin = validate_origin(origin)
    cert = ssl.PEM_cert_to_DER_cert((root/'cert.pem').read_text())
    key = secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode()).hexdigest()
    payload = json.dumps({'url':origin, 'pin':hashlib.sha256(cert).hexdigest(), 'key':key},
                         separators=(',', ':')).encode()
    iv = secrets.token_bytes(16)
    try:
        cipher = subprocess.run(
            ['/usr/bin/openssl', 'enc', '-aes-256-cbc', '-K', TICKET_ENCRYPTION_KEY, '-iv', iv.hex()],
            input=payload, capture_output=True, check=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('无法加密接入 Key，请检查 VPS 的 OpenSSL。') from error
    body = iv + cipher
    tag = hmac.digest(TICKET_AUTH_KEY, b'sc2.' + body, 'sha256')
    return 'sc2.' + base64.urlsafe_b64encode(body + tag).decode().rstrip('='), digest


def valid_activation_code(code):
    return (isinstance(code, str) and len(code) <= 128 and code.isascii() and
            hmac.compare_digest(hashlib.sha256(code.strip().encode()).digest(), ACTIVATION_CODE_SHA256))


def issue(root, origin, output, activation_code=None):
    if not (root/PUBLIC_ENABLED).is_file():
        raise ValueError(PUBLIC_DISABLED_MESSAGE)
    ticket,digest=make_key(root,origin)
    with output.open('x') as file:
        try:
            os.fchmod(file.fileno(),0o600)
            file.write(ticket+'\n')
            file.flush()
            KeyRegistry(root).issue(digest, activated=valid_activation_code(activation_code))
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    print('Key ID:',digest)


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True)
    cmds=parser.add_subparsers(dest='command',required=True)
    cmds.add_parser('serve')
    cmds.add_parser('admin')
    cmds.add_parser('bind-ssh-device')
    configure = cmds.add_parser('configure-port')
    configure.add_argument('expected_port', type=int)
    configure.add_argument('port', type=int)
    configure.add_argument('--rollback', action='store_true')
    create=cmds.add_parser('issue')
    create.add_argument('origin')
    create.add_argument('output',type=Path)
    create.add_argument('--prompt-activation', action='store_true')
    cmds.add_parser('revoke').add_argument('key_id')
    args=parser.parse_args()
    if args.command=='issue':
        try:
            from getpass import getpass
            code = getpass('激活码：') if args.prompt_activation else None
            issue(args.state,args.origin,args.output,code)
        except ValueError as error:
            parser.exit(1, str(error)+'\n')
    elif args.command=='revoke':
        if not re.fullmatch('[a-f0-9]{64}',args.key_id):parser.error('Invalid key ID')
        KeyRegistry(args.state).revoke(args.key_id)
    elif args.command=='bind-ssh-device':
        import sys
        try:
            options = json.loads(sys.stdin.buffer.read(4096))
            binding = OwnerHosts(args.state).bind(options['keyId'], options['hostClientId'], options['hostname'])
            print(json.dumps({'value':binding}))
        except (OSError, ValueError, KeyError, TypeError):
            print(json.dumps({'error':'无法将 Key 绑定到已验证的本机远程控制，请重新验证该设备。'}))
    elif args.command=='admin':
        import sys
        try:
            response=send_admin(args.state,sys.stdin.buffer.readline(8193))
            print(json.dumps(response,ensure_ascii=False))
        except (ValueError,OSError) as error:
            print(json.dumps({'error':str(error)},ensure_ascii=False))
    elif args.command == 'configure-port':
        try:
            configure_port(args.state, args.expected_port, args.port, check_available=not args.rollback)
        except (ValueError, OSError) as error:
            parser.exit(1, str(error)+'\n')
    else:
        with (args.state/'gateway.lock').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with open_public_server(args.state,json.loads((args.state/'config.json').read_text())) as server:
                admin_socket=args.state/'admin.sock'
                admin_socket.unlink(missing_ok=True)
                with socketserver.UnixStreamServer(str(admin_socket),AdminHandler) as admin:
                    admin_socket.chmod(0o600)
                    admin.manage=server.manage
                    worker=threading.Thread(target=admin.serve_forever,daemon=True)
                    worker.start()
                    signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
                    try:
                        server.serve_forever()
                    finally:
                        admin.shutdown()
                        admin_socket.unlink(missing_ok=True)
                        for manager in server.accounts.values():manager.close()
