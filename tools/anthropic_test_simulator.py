#!/usr/bin/env python3
"""214 测试机的最终 Anthropic 模拟层。产品与原版 CLI 保留 Claude 模型名。

仅在此处将模型映射为 DeepSeek Flash；不修改提示词、工具或推理参数。
不模拟 OAuth，不回退官方服务，不记录请求正文、响应正文或凭据。
"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
import pathlib
import platform
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = 'api.deepseek.com'
UPSTREAM_MODEL = 'deepseek-flash'
MAX_BODY = 64 * 1024 * 1024
STREAM_INTERRUPTED = {'type': 'error', 'error': {
    'type': 'api_error', 'message': 'Test upstream stream ended before message_stop; no fallback',
}}


class ClientDisconnected(Exception):
    pass


def map_request(body, models=None):
    data = json.loads(body)
    model = data.get('model')
    if not isinstance(model, str) or not model.startswith('claude-'):
        raise ValueError('模拟服务只接受原生 claude-* 模型标识')
    if models is not None and model.removesuffix('[1m]') not in {m['id'].removesuffix('[1m]') for m in models}:
        raise ValueError('模型不在已核对的测试目录中')
    data['model'] = UPSTREAM_MODEL
    return model, data


def map_response(data, model):
    # 只改协议中的模型标识；正文中的同名字符串必须保持原样。
    if data.get('type') == 'message' and isinstance(data.get('model'), str):
        data['model'] = model
    elif data.get('type') == 'message_start' and isinstance(data.get('message'), dict):
        message = data['message']
        if isinstance(message.get('model'), str):
            message['model'] = model
    return data


def stream_events(response, model):
    """Yield complete SSE frames and the type of any valid Anthropic event."""
    frame = []
    size = 0
    while line := response.readline(MAX_BODY + 1):
        frame.append(line)
        size += len(line)
        if size > MAX_BODY:
            raise ValueError('Upstream SSE event too large')
        if line not in (b'\n', b'\r\n'):
            continue
        data = []
        event_name = None
        for index, part in enumerate(frame):
            field, separator, value = part.rstrip(b'\r\n').partition(b':')
            if field == b'event' and separator:
                event_name = value.removeprefix(b' ')
            if field == b'data' and separator:
                data.append((index, value.removeprefix(b' ')))
        kind = None
        if data:
            try:
                event = json.loads(b'\n'.join(value for _, value in data))
            except (ValueError, RecursionError):
                event = None
            if isinstance(event, dict):
                kind = event.get('type')
                if kind in ('message_start', 'message'):
                    first = data[0][0]
                    ending = b'\r\n' if frame[first].endswith(b'\r\n') else b'\n'
                    frame[first] = b'data: ' + json.dumps(map_response(event, model), ensure_ascii=False).encode() + ending
                    for index, _ in reversed(data[1:]):
                        del frame[index]
                if kind in ('message_stop', 'error') and event_name not in (None, kind.encode()):
                    kind = None
        yield b''.join(frame), kind
        frame = []
        size = 0


class Simulator(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config):
        super().__init__(address, Handler)
        self.config = config
        self.audit_lock = threading.Lock()

    def audit(self, entry):
        with self.audit_lock:
            with open(self.config['audit'], 'a', encoding='utf-8') as out:
                out.write(json.dumps({'time': time.time(), **entry}, ensure_ascii=False) + '\n')


class Handler(BaseHTTPRequestHandler):
    server_version = 'SafeCodeTestSimulator/1'

    def log_message(self, *args):
        pass

    def json_reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_stream(self, body):
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as error:
            raise ClientDisconnected from error

    def relay_stream(self, response, model, entry):
        try:
            self.end_headers()
        except OSError as error:
            raise ClientDisconnected from error
        entry['downstreamStatus'] = response.status
        try:
            for frame, kind in stream_events(response, model):
                self.write_stream(frame)
                if kind == 'message_stop':
                    entry['streamComplete'] = True
                    return
                if kind == 'error':
                    entry.update(status=502, streamError=True)
                    return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        entry.update(status=502, transportError=True, streamIncomplete=True)
        self.write_stream(b'event: error\ndata: ' + json.dumps(STREAM_INTERRUPTED).encode() + b'\n\n')

    def do_GET(self):
        if self.path == '/health':
            return self.json_reply(200, {'ok': True, 'simulator': True,
                                        'upstream': UPSTREAM_HOST, 'upstreamModel': UPSTREAM_MODEL})
        if urllib.parse.urlsplit(self.path).path == '/v1/models':
            auth = self.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else self.headers.get('x-api-key', '')
            if not hmac.compare_digest(token, self.server.config['localToken']):
                return self.json_reply(401, {'error': {'type': 'authentication_error', 'message': 'Invalid local test token'}})
            models = self.server.config.get('models')
            if not models:return self.json_reply(503, {'error': {'type': 'api_error', 'message': 'Test model catalog not configured'}})
            return self.json_reply(200, {'data': models, 'has_more': False, 'first_id': models[0]['id'], 'last_id': models[-1]['id']})
        self.json_reply(404, {'error': {'type': 'not_found_error', 'message': 'Unsupported simulator endpoint'}})

    def do_POST(self):
        self.close_connection = True
        route = urllib.parse.urlsplit(self.path)
        if route.path not in ('/v1/messages', '/v1/messages/count_tokens'):
            return self.json_reply(404, {'error': {'type': 'not_found_error', 'message': 'Unsupported simulator endpoint'}})
        auth = self.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else self.headers.get('x-api-key', '')
        if not hmac.compare_digest(token, self.server.config['localToken']):
            return self.json_reply(401, {'error': {'type': 'authentication_error', 'message': 'Invalid local test token'}})
        if self.headers.get('Transfer-Encoding'):
            return self.json_reply(400, {'error': {'type': 'invalid_request_error', 'message': 'Content-Length required'}})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ValueError('Invalid body size')
            self.connection.settimeout(30)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete request')
            model, data = map_request(raw, self.server.config.get('models'))
        except (ValueError, TypeError, AttributeError, TimeoutError):
            return self.json_reply(400, {'error': {'type': 'invalid_request_error', 'message': 'Invalid Anthropic test request'}})
        entry = {'path': route.path, 'requestedModel': model, 'upstreamModel': UPSTREAM_MODEL,
                 'context1m': 'context-1m-2025-08-07' in {v.strip() for v in self.headers.get('anthropic-beta', '').split(',')},
                 'stream': data.get('stream', False), 'thinking': data.get('thinking', {}).get('type'),
                 'effort': data.get('output_config', {}).get('effort'),
                 'toolCount': len(data.get('tools', [])),
                 'temperature': data.get('temperature') if isinstance(data.get('temperature'), (int,float)) else None,
                 'maxTokens': data.get('max_tokens'),
                 'stopSequenceCount': len(data.get('stop_sequences', [])),
                 'hasOutputFormat': bool(data.get('output_config', {}).get('format')),
                 'classifier': model in self.server.config.get('classifierModels', []),
                 'requestId': hashlib.sha256(os.urandom(32)).hexdigest()[:16]}
        headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity',
                   'Authorization': 'Bearer ' + self.server.config['upstreamKey']}
        for name in ('anthropic-version', 'anthropic-beta', 'user-agent'):
            if name in self.headers:
                headers[name] = self.headers[name]
        upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=120)
        started = False
        try:
            upstream.request('POST', '/anthropic' + self.path,
                             body=json.dumps(data).encode(), headers=headers)
            response = upstream.getresponse()
            entry['status'] = response.status
            content_type = response.getheader('Content-Type', 'application/json')
            event_stream = content_type.partition(';')[0].strip().lower() == 'text/event-stream'
            if response.status == 200 and route.path == '/v1/messages' and data.get('stream') is True and not event_stream:
                raise ValueError('Upstream returned a non-streaming response')
            if response.status == 200 and event_stream:
                self.send_response(response.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                started = True
                entry['upstreamStatus'] = response.status
                self.relay_stream(response, model, entry)
            else:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise ValueError('Upstream response too large')
                if response.status == 200:
                    body = json.dumps(map_response(json.loads(body), model), ensure_ascii=False).encode()
                self.send_response(response.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                retry_after = response.getheader('Retry-After', '')
                if response.status >= 400 and 0 < len(retry_after) <= 128 and retry_after.isascii() and retry_after.isprintable():
                    self.send_header('Retry-After', retry_after)
                started = True
                try:
                    self.end_headers()
                    self.wfile.write(body)
                except OSError as error:
                    raise ClientDisconnected from error
        except ClientDisconnected:
            entry.update(status=None, clientDisconnected=True)
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            entry.update(status=502, transportError=True)
            if not started:
                try:
                    self.json_reply(502, {'error': {'type': 'api_error', 'message': 'Test upstream unavailable; no fallback'}})
                except OSError:
                    entry['clientDisconnected'] = True
        finally:
            upstream.close()
            self.server.audit(entry)


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.sendto(b'READY=1', address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=pathlib.Path)
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        raise SystemExit('模拟器配置必须仅文件所有者可读写')
    config = json.loads(args.config.read_text())
    if platform.node() != 'tom666' or config['hostname'] != 'tom666':
        raise SystemExit('此模拟器只允许在 214 的 tom666 测试 VM 上运行')
    os.umask(0o077)
    with Simulator(('127.0.0.1', config['port']), config) as server:
        notify_ready()
        server.serve_forever()


if __name__ == '__main__':
    main()
