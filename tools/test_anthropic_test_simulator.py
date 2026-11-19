"""验证模拟边界、原样保留工具内容，以及断网时禁止回退。"""
import copy
from collections import deque
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('simulator', Path(__file__).with_name('anthropic_test_simulator.py'))
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


@contextmanager
def serve(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(5)


class ScriptedUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        status, media_type, body, headers = self.server.responses.popleft()
        self.send_response(status)
        self.send_header('Content-Type', media_type)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class Tests(unittest.TestCase):
    def test_only_model_is_translated(self):
        original = {'model': 'claude-opus-5-5', 'thinking': {'type': 'adaptive'},
                    'output_config': {'effort': 'high'}, 'system': 'deepseek-flash',
                    'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                    'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'unchanged'}]}]}
        model, mapped = sim.map_request(json.dumps(original))
        self.assertEqual(model, original['model'])
        expected = copy.deepcopy(original)
        expected['model'] = 'deepseek-flash'
        self.assertEqual(mapped, expected)

    def test_only_response_model_metadata_is_translated(self):
        event = {'type': 'message_start', 'message': {'model': 'deepseek-flash', 'content': [{'type': 'text', 'text': 'deepseek-flash'}]}}
        actual = sim.map_response(event, 'claude-opus-5-5')
        self.assertEqual(actual['message']['model'], 'claude-opus-5-5')
        self.assertEqual(actual['message']['content'][0]['text'], 'deepseek-flash')

    def test_reject_bypassed_model_mapping(self):
        for model in ('deepseek-flash', 'https://api.anthropic.com', None):
            with self.assertRaises(ValueError):
                sim.map_request(json.dumps({'model': model}))

    def test_verified_catalog_accepts_all_models_and_refuses_unknown_versions(self):
        models=[{'id':m} for m in ('claude-opus-5-5','claude-fable-5-1','claude-sonnet-5-5','claude-haiku-5-5')]
        for row in models:
            for suffix in ('','[1m]'):
                requested,mapped=sim.map_request(json.dumps({'model':row['id']+suffix}),models)
                self.assertEqual(requested,row['id']+suffix)
                self.assertEqual(mapped['model'],'deepseek-flash')
        with self.assertRaises(ValueError):sim.map_request(json.dumps({'model':'claude-opus-nonexistent'}),models)

    def test_systemd_ready_message(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            address = str(Path(directory) / 'notify.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
                listener.bind(address)
                listener.settimeout(1)
                with patch.dict(os.environ, {'NOTIFY_SOCKET': address}):
                    sim.notify_ready()
                self.assertEqual(listener.recv(64), b'READY=1')

    def test_auth_routes_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            server = sim.Simulator(('127.0.0.1', 0), {'localToken': 'local-secret', 'upstreamKey': 'upstream-secret', 'audit': str(Path(directory)/'audit.jsonl')})
            audit_written = threading.Event()
            write_audit = server.audit

            def record_audit(entry):
                write_audit(entry)
                audit_written.set()

            server.audit = record_audit
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, token):
                client = http.client.HTTPConnection(*server.server_address, timeout=5)
                client.request('POST', path, json.dumps({'model': 'claude-opus-5-5', 'messages': []}), {'Authorization': 'Bearer '+token})
                response = client.getresponse()
                status, body = response.status, response.read()
                client.close()
                return status, body
            try:
                client=http.client.HTTPConnection(*server.server_address,timeout=5)
                client.request('GET','/v1/models');response=client.getresponse()
                self.assertEqual(response.status,401);response.read();client.close()
                server.config['models']=[{'id':'claude-opus-5-5','display_name':'Opus'}]
                client=http.client.HTTPConnection(*server.server_address,timeout=5)
                client.request('GET','/v1/models',headers={'Authorization':'Bearer local-secret'});response=client.getresponse()
                self.assertEqual(response.status,200)
                catalog=response.read();self.assertEqual(json.loads(catalog)['data'],server.config['models'])
                self.assertNotIn(b'secret',catalog);client.close()
                with patch.object(sim.http.client, 'HTTPSConnection') as upstream:
                    upstream.return_value.request.side_effect = OSError('offline')
                    self.assertEqual(request('/v1/messages', 'wrong')[0], 401)
                    upstream.assert_not_called()
                    self.assertEqual(request('/anything', 'local-secret')[0], 404)
                    upstream.assert_not_called()
                    status, body = request('/v1/messages?beta=true', 'local-secret')
                    self.assertEqual(status, 502)
                    self.assertIn(b'no fallback', body)
                    upstream.assert_called_once_with('api.deepseek.com', timeout=120)
                    call = upstream.return_value.request.call_args
                    self.assertEqual(call.args[1], '/anthropic/v1/messages?beta=true')
                    self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer upstream-secret')
                self.assertTrue(audit_written.wait(5))
                audit = (Path(directory)/'audit.jsonl').read_text()
                self.assertNotIn('secret', audit)
                self.assertNotIn('messages', json.loads(audit))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_stream_outage_error_retry_after_and_recovery(self):
        start = (
            b': keepalive\r\n\r\nevent: message_start\r\nid: fixture\r\n'
            b'data: {"type":"message_start",\r\ndata: "message":{"model":"deepseek-flash"}}\r\n\r\n'
        )
        stop = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        upstream_error = (
            b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"fixture"}}\n\n'
        )
        broken_chunked = f'{len(start):x}'.encode() + b'\r\n' + start + b'\r\n20\r\nshort'
        failures = b'{"type":"error","error":{"type":"api_error","message":"fixture"}}'
        sse = 'text/event-stream; charset=utf-8'
        with tempfile.TemporaryDirectory() as directory, serve(
            ThreadingHTTPServer(('127.0.0.1', 0), ScriptedUpstream)
        ) as upstream, serve(sim.Simulator(('127.0.0.1', 0), {
            'localToken': 'fixture-token', 'upstreamKey': 'fixture-upstream-key',
            'audit': str(Path(directory) / 'audit.jsonl'),
        })) as server:
            upstream.responses = deque([
                (200, sse, start + stop, {}),
                (200, sse, start + stop[:-1], {}),
                (200, sse, broken_chunked, {'Transfer-Encoding': 'chunked'}),
                (200, sse, start + upstream_error, {}),
                (429, 'application/json', failures, {'Retry-After': '120'}),
                (503, 'application/json', failures, {'Retry-After': 'Thu, 19 Nov 2026 12:00:00 GMT'}),
                (200, 'application/json', b'{"type":"message","model":"deepseek-flash"}', {}),
                (200, sse, start + stop, {}),
            ])

            audited = threading.Event()
            write_audit = server.audit

            def record_audit(entry):
                write_audit(entry)
                audited.set()

            server.audit = record_audit

            def request():
                audited.clear()
                client = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    client.request('POST', '/v1/messages', json.dumps({'model': 'claude-opus-5-5', 'stream': True}),
                                   {'Authorization': 'Bearer fixture-token'})
                    response = client.getresponse()
                    result = response.status, response.getheader('Retry-After'), response.read()
                    self.assertTrue(audited.wait(5))
                    return result
                finally:
                    client.close()

            def connect_upstream(host, timeout):
                return http.client.HTTPConnection(*upstream.server_address, timeout=5)

            with patch.object(sim.http.client, 'HTTPSConnection', side_effect=connect_upstream) as connect:
                complete, clean_eof, broken, error, limited, unavailable, wrong_content, recovered = (request() for _ in range(8))
                self.assertEqual(connect.call_count, 8)
                self.assertTrue(all(call.args == ('api.deepseek.com',) for call in connect.call_args_list))

            self.assertEqual(complete, recovered)
            self.assertEqual(complete[:2], (200, None))
            self.assertTrue(complete[2].startswith(b': keepalive\r\n\r\nevent: message_start\r\nid: fixture\r\n'))
            self.assertIn(b'"model": "claude-opus-5-5"', complete[2])
            self.assertTrue(complete[2].endswith(stop))
            for partial in (clean_eof, broken):
                self.assertEqual(partial[:2], (200, None))
                self.assertIn(b'"model": "claude-opus-5-5"', partial[2])
                self.assertEqual(partial[2].count(b'event: error'), 1)
                self.assertNotIn(b'event: message_stop', partial[2])
                error_event = json.loads(partial[2].split(b'event: error\ndata: ', 1)[1])
                self.assertEqual(error_event, sim.STREAM_INTERRUPTED)
            self.assertTrue(error[2].endswith(upstream_error))
            self.assertEqual(error[2].count(b'event: error'), 1)
            self.assertEqual(limited, (429, '120', failures))
            self.assertEqual(unavailable, (503, 'Thu, 19 Nov 2026 12:00:00 GMT', failures))
            self.assertEqual(wrong_content[0], 502)
            audit = [json.loads(line) for line in (Path(directory) / 'audit.jsonl').read_text().splitlines()]
            self.assertEqual([item['status'] for item in audit], [200, 502, 502, 502, 429, 503, 502, 200])
            self.assertEqual([item.get('streamComplete') for item in audit], [True, None, None, None, None, None, None, True])
            for item in audit[1:3]:
                self.assertTrue(item['transportError'] and item['streamIncomplete'])
                self.assertEqual((item['upstreamStatus'], item['downstreamStatus']), (200, 200))
            self.assertTrue(audit[3]['streamError'])

    def test_disconnected_stream_client_gets_no_second_response(self):
        body = json.dumps({'model': 'claude-opus-5-5', 'stream': True}).encode()
        handler = sim.Handler.__new__(sim.Handler)
        handler.path = '/v1/messages'
        handler.headers = {'Authorization': 'Bearer fixture-token', 'Content-Length': str(len(body))}
        handler.rfile, handler.wfile, handler.connection = io.BytesIO(body), Mock(), Mock()
        handler.server = Mock(config={'localToken': 'fixture-token', 'upstreamKey': 'fixture-upstream-key'})
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler.wfile.write.side_effect = BrokenPipeError
        response = io.BytesIO(b'event: ping\ndata: {"type":"ping"}\n\n')
        response.status = 200
        response.getheader = Mock(return_value='text/event-stream')
        with patch.object(sim.http.client, 'HTTPSConnection') as upstream:
            upstream.return_value.getresponse.return_value = response
            handler.do_POST()
        handler.send_response.assert_called_once_with(200)
        handler.wfile.write.assert_called_once()
        entry = handler.server.audit.call_args.args[0]
        self.assertTrue(entry['clientDisconnected'])
        self.assertIsNone(entry['status'])
        self.assertNotIn('streamComplete', entry)


if __name__ == '__main__':
    unittest.main()
