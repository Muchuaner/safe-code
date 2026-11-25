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
from desktop_gateway import CLOUDCLI_MODEL_ID, CLOUDCLI_REQUIRED, COUNT_TOKENS_PATH, MAX_MESSAGES_REQUEST, MESSAGES_PATH, CloudCliBackend, CloudCliError, GatewayError, Handler, Manager, diagnostic, load_cloudcli_config
from device_bridge import (DeviceAlreadyBound, DeviceError, DeviceOffline, Devices, OwnerHosts, SSH_DEVICE,
                           paired_device, pairing_lock, read_json, device_service_problem)
from management import ANALYTICS_RANGES, KEY_ID, TOKEN_FIELDS, Capacity, KeyRegistry, RequestQueue, peer_disconnected, read_admin_line, send_admin
from pricing import usage_price
from prompt_review import PromptReviewer

PUBLIC_ENABLED = 'public-enabled'
PUBLIC_ORIGIN = 'public-origin'
PUBLIC_CONCURRENCY = 'public-concurrency'
GATEWAY_REVISION = 26
PUBLIC_KEY_QUEUE = 'public-key-queue'
PUBLIC_DISABLED_MESSAGE = '请先在 Safe Code 中启用公网网关。'
# A shared promotional code, checked only at the gateway.
ACTIVATION_CODE_SHA256 = bytes.fromhex('89debb851e3fc69c8e163a3b6462a3493e93d38ae8cf0132e419bd9e0b32d2b9')
# Shared with DesktopKeyAccess. These bundled keys deter casual inspection only.
TICKET_ENCRYPTION_KEY = 'f04223626f9a64f32aa618705067a6a04a5d852f8e5b551c944d0b9cd2d7f54d'
TICKET_AUTH_KEY = bytes.fromhex('bf8145e375d11db0fdd2fb0d9734fdda504c5a480fc2eae4a567c1948aa86155')


class PublicManager(Manager):
    unrestricted_tools = True

    def __init__(self, folder, config, *, require_cloudcli=True, review_prompt=None):
        super().__init__(folder, config, require_cloudcli=require_cloudcli, review_prompt=review_prompt)
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



def relay_frp(client, upstream, still_authorized):
    # FRP starts sending only after CONNECT. Check revocation even on an idle stream.
    last_activity = time.monotonic()
    next_check = last_activity + 2
    while True:
        now = time.monotonic()
        if now >= next_check:
            if not still_authorized():
                return
            next_check = now + 2
        if now - last_activity >= 90:
            return
        ready = [client] if client.pending() else []
        readable, _, _ = select.select([client, upstream], [], [], 0 if ready else max(0,next_check-now))
        for source in set(ready + readable):
            data = source.recv(65536)
            if not data:
                return
            destination = upstream if source is client else client
            destination.sendall(data)
            last_activity = time.monotonic()


class PublicHandler(Handler):
    timeout = 12

    def setup(self):
        self.request.settimeout(self.timeout)
        super().setup()

    @property
    def manager(self):
        return self.server.account(self.digest, self.platform, self.device)

    def credential_digest(self):
        value = self.headers.get('Authorization', '')
        token = value[7:] if value.startswith('Bearer ') else self.headers.get('x-api-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        return hashlib.sha256(token.encode()).hexdigest()

    def authorized(self):
        digest = self.credential_digest()
        if digest is None:
            return False
        if not self.server.registry.enabled(digest):
            return False
        platform = self.headers.get('X-SafeCode-Platform', 'windows')
        if platform not in ('windows', 'macos'):
            return False
        self.digest, self.platform = digest, platform
        return True

    def do_GET(self):
        self.device = None
        if self.path == '/v1/key/balance':
            if not self.server.public_enabled:
                return self.public_disabled()
            if not self.authorized():
                return self.device_error(401, 'SafeCode Key required')
            balance = self.server.registry.balance(self.digest)
            if balance is None:
                return self.device_error(401, 'SafeCode Key required')
            return self.reply(200, balance)
        if self.path.partition('?')[0] == '/v1/cloudcli/models':
            self.path = self.path.replace('/v1/cloudcli/', '/v1/', 1)
        if self.path.partition('?')[0] != '/v1/models':
            return self.reply(404, {'error':{'message':'Unknown endpoint'}})
        if not self.server.public_enabled:
            return self.public_disabled()
        if not self.authorized():
            return self.device_error(401, 'SafeCode Key required')
        if self.server.config['cloudcli'] is None:
            return self.backend_required()
        try:
            allowed = self.server.registry.model_policy(self.digest)
            manager = self.manager
            return self.reply(200, manager.catalog() if allowed is None else manager.cloudcli.catalog(allowed))
        except GatewayError as error:
            return self.device_error(error.status, str(error))
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            return self.device_error(503, 'CloudCLI model discovery unavailable')

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
                relay_frp(self.connection, upstream,lambda: self.device_lease_active(row))
            except OSError:
                pass

    def device_lease_active(self, row):
        try:
            current = self.server.devices.enrolled(self.digest,self.platform,row['id'],
                                                    self.headers.get('X-SafeCode-Device-Proof'))
            return (self.server.public_enabled and current['expires'] > time.time()
                    and self.server.registry.enabled(self.digest))
        except (DeviceError,OSError,sqlite3.Error):
            return False

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
            return self.reject_device_message(401, 'SafeCode API Key 无效或已停用。', 'key_rejected')
        if self.server.config['cloudcli'] is None:
            return self.reject_device_message(503, CLOUDCLI_REQUIRED)
        if self.headers.get('X-SafeCode-Diagnostics') == '1':
            active, waiting, slots = self.server.queue.snapshot()
            diagnostic(backend='cloudcli' if self.server.config['cloudcli'] else 'claude_code',
                       active=min(1000000, sum(active.values())), waiting=min(1000000, sum(waiting.values())),
                       slots=min(1000000, slots))
        if device is None and proof is None:
            try:
                required = self.requires_device()
            except (OSError, sqlite3.Error):
                return self.reject_device_message(503, '暂时无法检查本机设备状态，请稍后重试。', 'device_state_unavailable')
            if required:
                return self.reject_device_message(403, '请先在 Safe Code 完成本机设备配对。', 'device_pairing_required')
        else:
            diagnostic(stage='device_check')
            binding = self.server.owner_hosts if SSH_DEVICE.fullmatch(device or '') else self.server.devices
            if binding is None:
                return self.reject_device_message(503, '网关的本机连接服务尚未就绪，请联系管理员。',
                                                  'device_service_unavailable')
            try:
                self.device = binding.chat_binding(self.digest, self.platform, device, proof)
            except DeviceOffline:
                return self.reject_device_message(503, '本机连接暂不可用。请在 Safe Code 恢复设备连接后继续。',
                                                  'device_offline')
            except DeviceAlreadyBound as error:
                return self.reject_device_message(409, str(error), 'device_already_bound')
            except DeviceError:
                return self.reject_device_message(403, '本机设备授权无效。请在 Safe Code 重新验证设备。',
                                                  'device_pairing_rejected')
            except (OSError, sqlite3.Error):
                return self.reject_device_message(503, '暂时无法检查本机设备状态，请稍后重试。',
                                                  'device_state_unavailable')
        super().handle_messages()

    def requires_device(self):
        return (self.headers.get('X-SafeCode-Client') == 'safecode-desktop'
                or (self.server.root/'device-required'/self.digest).is_file()
                or (self.server.root/'pairing-enforced'/self.digest).is_file()
                or paired_device(self.server.root, self.digest) is not None)

    def verify_admitted_device(self):
        if not self.device:
            if self.requires_device():
                raise GatewayError('这把 Key 已绑定设备，请从原设备发起请求。', 403)
            return
        device = self.device['id']
        proof = self.headers.get('X-SafeCode-Device-Proof')
        try:
            if SSH_DEVICE.fullmatch(device):
                if paired_device(self.server.root, self.digest) != device:
                    raise DeviceAlreadyBound('设备绑定已变更')
                self.server.owner_hosts.authenticate(self.digest, self.platform, device, proof)
            else:
                row = self.server.devices.enrolled(self.digest, self.platform, device, proof)
                if row['expires'] <= time.time():
                    raise DeviceOffline('设备连接已超时')
        except DeviceOffline as error:
            raise GatewayError(str(error), 503) from None
        except DeviceError:
            raise GatewayError('设备授权已失效，请在 Safe Code 重新验证设备。', 403) from None

    def reject_device_message(self, status, message, code=None):
        # Read a bounded request before closing TLS so the rejection body reaches the relay.
        try:
            remaining = int(self.headers.get('Content-Length', '0'))
            if not self.headers.get('Transfer-Encoding') and 0 < remaining <= MAX_MESSAGES_REQUEST:
                while remaining:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
        except (ValueError, OSError):
            pass
        return self.device_error(status, message, code)

    def device_error(self, status, message, code=None):
        kind = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_error'}.get(status, 'api_error')
        error = {'type': kind, 'message': message}
        if code:
            error['code'] = code
        return self.reply(status, {'type': 'error', 'error': error})

    def device_control(self):
        authorized = self.authorized()
        if not authorized and self.credential_digest() is None:
            return self.device_error(401, 'SafeCode Key required')
        try:
            request = read_json(self)
            if not authorized:
                digest = self.credential_digest()
                if request.get('action') == 'revoke' and digest and not self.server.registry.has_marker(digest):
                    # No active credential exists. Acknowledge only local teardown, including retries after deletion.
                    return self.reply(200, {'revoked': True})
                return self.device_error(401, 'SafeCode Key required')
            action = request.get('action')
            device = request.get('deviceId')
            if action == 'revoke' and isinstance(device, str) and SSH_DEVICE.fullmatch(device):
                self.server.owner_hosts.revoke(self.digest, self.platform, device, request.get('deviceProof'))
                return self.reply(200, {'revoked':True})
            devices = self.server.devices
            if devices is None:
                return self.device_error(403, 'Private device service is not configured', 'device_not_configured')
            if not devices.permitted(self.digest):
                return self.device_error(403, 'Device control is unavailable for this Key', 'device_not_allowed')
            if action in ('capability','prepare'):
                problem = device_service_problem(self.server.config)
                if problem:
                    return self.device_error(503, 'Private device service is unavailable', problem)
            if action == 'capability':
                return self.reply(200, {'supported': True, 'transport': 'frp', 'httpsTunnel': True, 'port': self.server.frp_port,
                                        'deviceRequired': devices.required(self.digest)})
            if action == 'prepare':
                if request.get('accepted') is not True:
                    raise DeviceError('Local control consent is required')
                return self.reply(200, devices.prepare(self.digest, self.platform, request.get('hostname')))
            row = devices.enrolled(self.digest, self.platform, request.get('deviceId'), request.get('deviceProof'),
                                   require_current=action != 'revoke', missing_ok=action == 'revoke')
            if action == 'revoke':
                if row is not None:
                    devices.revoke(row)
                return self.reply(200, {'revoked': True})
            if action == 'verify':
                try:
                    command = devices.configure(row, request.get('username'), request.get('hostKey'))
                except DeviceError:
                    return self.device_error(400, 'SafeCode could not verify the SSH return path',
                                             'device_ssh_verification_failed')
                return self.reply(200, {'command':command})
            if action == 'renew':
                devices.renew(row)
                return self.reply(200, {'ready': bool(row['ready'])})
            raise DeviceError('Unknown device action')
        except DeviceAlreadyBound as error:
            return self.device_error(409, str(error), 'device_already_bound')
        except (DeviceError, ValueError, TypeError):
            return self.device_error(400, 'SafeCode could not apply the device request')
        except (OSError, sqlite3.Error):
            return self.device_error(503, 'SafeCode device control is unavailable')

    def public_disabled(self):
        self.close_connection = True
        return self.reply(403, {'error': {'type': 'permission_error', 'message': PUBLIC_DISABLED_MESSAGE}})

    def backend_required(self):
        return self.reply(503, {'type': 'error', 'error': {'type': 'api_error', 'message': CLOUDCLI_REQUIRED}})

    def check_model(self, model):
        allowed = self.server.registry.model_policy(self.digest)
        if allowed is not None and model not in allowed:
            raise CloudCliError('This model is not allowed for this API Key', 403)

    def execute_message(self, data, **options):
        if not isinstance(data,dict):
            raise GatewayError('Invalid Messages request',400)
        requested = data.get('model')
        model = requested if isinstance(requested, str) and CLOUDCLI_MODEL_ID.fullmatch(requested) else 'unknown'
        enqueued = time.monotonic()
        started = None
        record = {'model': model, 'outcome': 'success', 'status': 200, 'wait_ms': 0, 'duration_ms': None}
        diagnostic(stage='gateway_queue')
        try:
            stream = options.get('stream')
            manager = self.server.account(self.digest, self.platform, self.device, create=False)
            if manager is not None and manager.cloudcli is not None:
                manager.cloudcli.request_previous_stop(data, options.get('session_header'))
            with self.server.queue.slot(self.digest, lambda: peer_disconnected(self.connection),
                                        on_wait=stream.queue_ping if stream is not None else None):
                self.verify_admitted_device()
                started = time.monotonic()
                diagnostic(stage='gateway_validation', waitMs=round((started-enqueued)*1000))
                manager = self.manager
                manager.accounting.before = {}
                manager.accounting.model = model
                manager.accounting.finished = None
                try:
                    return manager.run(data, **options, check_model=self.check_model)
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
        questions = cloudcli.get('desktopQuestions') if cloudcli else None
        if cloudcli and not ipaddress.ip_address(config['bind']).is_loopback and (
                cloudcli.get('hostControl') or
                questions and ('tokenFile' in questions or not cloudcli.get('desktopToolApprovals'))):
            raise ValueError('Direct CloudCLI gateways cannot use a shared workstation or disable configured Desktop tool approvals.')
        self.root, self.config = root, {**config, 'cloudcli': cloudcli}
        self.review_prompt = PromptReviewer(root, cloudcli)
        self.frp_port = config.get('deviceFrpPort')
        if self.frp_port is not None and (type(self.frp_port) is not int or not 1024 <= self.frp_port <= 65535):
            raise ValueError('Invalid private FRP control port')
        self.devices = Devices(root) if cloudcli and self.frp_port else None
        self.owner_hosts = OwnerHosts(root)
        self.accounts = {}
        self.lock = threading.Lock()
        self.usage_lock = threading.Lock()
        self.subscription = {'state': 'unavailable', 'windows': {}}
        self.subscription_path = root/'subscription-snapshot.json'
        try:
            self.subscription = self.subscription_snapshot(json.loads(self.subscription_path.read_text()))
        except (OSError, ValueError):
            pass
        self.registry = KeyRegistry(root)
        self.catalog_backend = None
        self.registry.prune_requests()
        self.next_log_cleanup = time.monotonic() + 3600
        try:
            concurrency = int((root/PUBLIC_CONCURRENCY).read_text())
        except (FileNotFoundError, ValueError):
            concurrency = 0
        self.queue = RequestQueue(self.registry, concurrency=max(0, concurrency),
                                  key_mode=(root/PUBLIC_KEY_QUEUE).is_file())
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

    def account(self, digest, platform, device=None, *, create=True):
        identity = digest+'-'+platform+('-'+device['id'] if device else '')
        with self.lock:
            if identity not in self.accounts:
                if not create:
                    return None
                folder = self.root/'sessions'/identity
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                m = PublicManager(folder, {**self.config, 'instance':digest[:32], 'clientPlatform':platform,
                                           'pairedDevice':device}, review_prompt=self.review_prompt)
                m.account_footer = lambda model, usage: self.account_footer(digest, model, usage)
                if m.cloudcli:
                    m.cloudcli.parallel = True
                m.live_slots = self.turns
                m.webfetch_slots = self.webfetch_turns
                self.accounts[identity] = m
            return self.accounts[identity]

    @staticmethod
    def subscription_snapshot(sample):
        if not isinstance(sample, dict) or type(sample.get('sampled_at')) is not int or not isinstance(sample.get('windows'), dict):
            raise ValueError('无效的额度快照。')
        windows = {}
        for key in ('five_hour', 'seven_day'):
            window = sample['windows'].get(key)
            if not isinstance(window, dict):
                continue
            used = window.get('used')
            if type(used) not in (int, float) or not 0 <= used <= 100:
                continue
            windows[key] = {'used': used}
            if type(window.get('resets_at')) is int:
                windows[key]['resets_at'] = window['resets_at']
        if not windows:
            raise ValueError('额度快照缺少有效窗口。')
        return {'state': 'available', 'windows': windows, 'sampled_at': sample['sampled_at']}

    def remember_subscription(self, sample):
        value = self.subscription_snapshot(sample)
        if value['sampled_at'] >= self.subscription.get('sampled_at', 0):
            atomic_write(self.subscription_path, json.dumps(value))
            self.subscription = value
        return self.subscription

    def account_footer(self, digest, model, usage):
        # The response is saved for replay before the handler posts the charge.
        # Include this turn once, using the same prices as the ledger.
        amount = '暂不可用'
        try:
            balance = self.registry.balance(digest)
            if balance is not None:
                remaining = balance['remaining_nanos']
                if remaining is None:
                    amount = '不限额'
                else:
                    cost, _ = usage_price(model, usage)
                    nanos = max(0, remaining - cost)
                    amount = '< $0.0001' if 0 < nanos < 100_000 else f'${nanos / 1_000_000_000:.4f}'
        except sqlite3.Error:
            pass

        sample = self.subscription
        windows = sample['windows']
        values = []
        for key, label in (('five_hour', '5 小时'), ('seven_day', '7 天')):
            window = windows.get(key, {})
            used = window.get('used')
            reset = window.get('resets_at')
            if type(used) in (int, float) and 0 <= used <= 100 and (not reset or reset > time.time()):
                values.append(f'{label}剩余 {int(100 - used + 0.5)}%')
            else:
                values.append(f'{label}暂无记录')
        quota = ' · '.join(values) if windows else '暂无记录'
        return (f'Key 余额 {amount}', f'账户额度（上次查询） · {quota}')

    def manage(self, request):
        operation = request.get('action')
        if operation == 'public_status':
            status = {'enabled': self.public_enabled, 'backend': 'cloudcli' if self.config['cloudcli'] else 'unconfigured',
                      'gatewayRevision': GATEWAY_REVISION}
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
            return {'enabled': self.public_enabled, 'backend': 'cloudcli', 'gatewayRevision': GATEWAY_REVISION}
        if not self.public_enabled:
            raise ValueError(PUBLIC_DISABLED_MESSAGE)
        if operation == 'set_key_queue':
            enabled = request.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('请选择是否开启 API 排队。')
            with self.lock:
                if enabled:
                    atomic_write(self.root/PUBLIC_KEY_QUEUE, '1\n')
                else:
                    (self.root/PUBLIC_KEY_QUEUE).unlink(missing_ok=True)
                self.queue.set_key_mode(enabled)
                return self.queue.key_status()
        if operation == 'set_prompt_review':
            with self.lock:
                return self.review_prompt.update(request, atomic_write)
        if operation == 'model_catalog':
            if self.config['cloudcli'] is None:
                raise ValueError(CLOUDCLI_REQUIRED)
            with self.lock:
                if self.catalog_backend is None:
                    folder = self.root/'model-catalog'
                    folder.mkdir(mode=0o700, exist_ok=True)
                    self.catalog_backend = CloudCliBackend(folder, self.config['cloudcli'])
            return {'models': self.catalog_backend.managed_models()}
        if operation == 'remember_subscription':
            with self.usage_lock:
                return self.remember_subscription(request.get('sample'))
        if operation == 'refresh_subscription':
            if not self.usage_lock.acquire(blocking=False):
                return {'state': 'loading', 'windows': self.subscription['windows']}
            try:
                try:
                    windows = read_subscription(timeout=6)
                except (OSError, ValueError, AttributeError, TypeError):
                    windows = {}
                if windows:
                    self.remember_subscription({'windows': windows, 'sampled_at': int(time.time())})
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
                key['bound_device'] = paired_device(self.root, key['id'])
                key['device_access'] = (self.root/'device-access'/key['id']).is_file()
            result = {'keys':keys,'origin':self.public_origin,
                      'activation_required':any(key['enabled'] for key in keys),
                      'can_delete_revoked_keys': True, 'key_policy_supported': True, 'device_policy_supported': True,
                      'device_service_problem': device_service_problem(self.config),
                      'billing': {'cost_nanos': sum(key['cost_nanos'] for key in keys),
                                  'unpriced_tokens': sum(key['unpriced_tokens'] for key in keys)},
                      'active':bool(active),'active_count':sum(active.values()),'waiting':sum(waiters.values()),
                      'concurrency':concurrency,'concurrency_text':str(concurrency),'queue_limit':self.queue.max_queue,
                      'wait_limit':self.queue.max_wait,
                      'key_queue':self.queue.key_status(), 'prompt_review':self.review_prompt.status()}
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
            device_access = request.get('device_access')
            if 'device_access' in request and type(device_access) is not bool:
                raise ValueError('请选择有效的设备配对权限。')
            if device_access is True and (self.devices is None or device_service_problem(self.config)):
                raise ValueError('私有设备服务尚未就绪，请在网关设置中安装或修复服务。')
            settings = {'budget_nanos': None if budget is None else budget * 10_000_000} if 'budget_cents' in request else {}
            if 'expires_at' in request:
                expiry = request['expires_at']
                if expiry is not None and (type(expiry) is not int or not 1 <= expiry <= 253402300799):
                    raise ValueError('请选择有效的失效时间。')
                settings['expires_at'] = expiry
            if 'allowed_models' in request:
                models = request['allowed_models']
                if models is not None:
                    if (not isinstance(models, list) or not models or
                            any(not isinstance(model, str) or not CLOUDCLI_MODEL_ID.fullmatch(model) for model in models)):
                        raise ValueError('请至少选择一个允许使用的模型。')
                    models = json.dumps(list(dict.fromkeys(models)), separators=(',', ':'))
                settings['allowed_models'] = models
            if operation=='issue':
                if self.config['cloudcli'] is None:
                    raise ValueError(CLOUDCLI_REQUIRED)
                origin = self.public_origin or request.get('origin')
                if not origin:
                    raise ValueError('无法识别 VPS 的公网 IP，请从 Safe Code 的 SSH 会话创建密钥。')
                ticket, key_id = make_key(self.root,origin)
                self.registry.issue(key_id, note.strip(), rpm, activated=valid_activation_code(request.get('activation_code')), **settings)
                if device_access:
                    try:
                        self.devices.set_access(key_id, True)
                    except OSError:
                        self.registry.revoke(key_id)
                        self.registry.delete(key_id)
                        raise ValueError('无法保存 Key 的设备配对权限。') from None
                return {'id':key_id,'ticket':ticket}
        key_id = request.get('id','')
        if not isinstance(key_id,str) or not KEY_ID.fullmatch(key_id):
            raise ValueError('Key ID 无效。')
        if operation=='usage':
            return {'models':self.registry.usage(key_id), 'recent':self.registry.recent_requests(key_id)}
        if operation=='update':
            self.registry.update(key_id,note.strip(),rpm,**settings)
            if device_access is not None:
                if self.devices is not None:
                    self.devices.set_access(key_id,device_access)
                elif device_access is False:
                    (self.root/'device-access'/key_id).unlink(missing_ok=True)
        elif operation == 'unbind':
            expected = request.get('deviceId')
            if not isinstance(expected, str) or not expected:
                raise ValueError('请刷新列表后选择要解绑的设备。')
            with pairing_lock(self.root, key_id):
                current = paired_device(self.root, key_id)
                if current is not None and current != expected:
                    raise ValueError('设备绑定已变更，请刷新列表后重试。')
                if current is not None:
                    # Continue to require device proof while the administrator changes devices.
                    policy = self.root/'pairing-enforced'
                    policy.mkdir(mode=0o700, exist_ok=True)
                    (policy/key_id).touch(mode=0o600, exist_ok=True)
                    devices = self.devices
                    if devices is None and (self.root/'device-bridge.sqlite3').exists():
                        devices = Devices(self.root)
                    if devices is not None:
                        devices.clear_pairings(key_id)
                    for path in (self.root/'ssh-devices').glob(key_id+'-ssh-*'):
                        if SSH_DEVICE.fullmatch(path.name[len(key_id)+1:]):
                            path.unlink(missing_ok=True)
        elif operation=='revoke':
            self.registry.revoke(key_id)
            if (self.root/'device-access'/key_id).is_file():
                if self.devices is not None:
                    self.devices.set_access(key_id,False)
                else:
                    (self.root/'device-access'/key_id).unlink(missing_ok=True)
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
        except DeviceAlreadyBound as error:
            print(json.dumps({'error':str(error)},ensure_ascii=False))
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
