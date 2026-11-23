"""Run the Desktop Messages gateway against CloudCLI-compatible loopback fixtures."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / 'src/SafeCode.Core/Remote'
PUBLIC = ROOT / 'tools/desktop-api'
sys.path[:0] = [str(REMOTE), str(PUBLIC)]
import desktop_gateway as gateway
import configure_cloudcli as setup_cloudcli

public_spec = importlib.util.spec_from_file_location('public_gateway_fixture', PUBLIC / 'server.py')
public_gateway = importlib.util.module_from_spec(public_spec)
public_spec.loader.exec_module(public_gateway)

API_KEY = 'cloudcli-private-fixture-key'
DESKTOP_TOKEN = 'desktop-local-fixture-token'
MODEL = 'claude-haiku-5-5'
CLIENT_ID = 'a' * 20
CLOUD_SESSION = 'app-cloudcli-fixture-session'


def conversation(prompt, **overrides):
    return {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 128000,
        'output_config': {'effort': 'medium'},
        **overrides,
    }


def desktop_environment(directory):
    return (
        '# Environment\nYou have been invoked in the following environment:\n'
        f'- Primary working directory: {directory}'
    )


def cloud_turn(text='Fixture reply', *, deltas=False, session_id=CLOUD_SESSION):
    frames = [{'type': 'status', 'message': 'Session started'},
              {'type': 'session-id', 'sessionId': session_id}]
    if deltas:
        frames.extend([
            {'kind': 'stream_delta', 'content': 'Fixture '},
            {'kind': 'stream_delta', 'content': 'reply'},
            {'kind': 'stream_end'},
        ])
    frames.extend([
        {'kind': 'text', 'role': 'user', 'content': 'User event, not an assistant answer'},
        {'kind': 'text', 'role': 'assistant', 'content': text},
        {'kind': 'status', 'text': 'token_budget', 'tokenBudget': {
            'inputTokens': 100, 'outputTokens': 20, 'cacheReadTokens': 17, 'cacheCreationTokens': 8,
        }},
        {'kind': 'complete', 'exitCode': 0},
        {'type': 'done'},
    ])
    return frames


class CloudAgentFixture(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.split('?')[0] == f'/api/providers/sessions/{CLOUD_SESSION}/messages':
            query = parse_qs(urlsplit(self.path).query)
            limit = int(query.get('limit', ['64'])[0])
            offset = int(query.get('offset', ['0'])[0])
            history = self.server.history
            end = max(0, len(history) - offset)
            self.send_json({'data': {'messages': history[max(0, end - limit):end],
                                     'offset': offset, 'total': len(history), 'hasMore': end > limit}})
            return
        if self.path == '/api/providers/sessions/running':
            started = self.server.running_since
            running = [{'sessionId': CLOUD_SESSION, 'provider': 'claude', 'startedAt': started}] if started is not None else []
            self.send_json({'data': {'sessions': running}})
            return
        if self.path != '/api/providers/claude/models':
            self.send_error(404)
            return
        self.server.model_requests.append(self.headers.get('Authorization'))
        payload = json.dumps({'data': {'models': {'OPTIONS': self.server.model_options,
                    'CUSTOM_MODELS_ONLY': self.server.custom_models_only}}}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        server = self.server
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/api/assets/files':
            content_type = self.headers['Content-Type']
            form = BytesParser(policy=policy.default).parsebytes(f'Content-Type: {content_type}\r\n\r\n'.encode()+raw)
            uploads = [(part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
                       for part in form.iter_parts()]
            server.uploads.extend(uploads)
            self.send_json({'attachments': [{'name': name, 'mimeType': mime, 'size': len(data),
                                             'path': f'/example/assets/{index}-{name}'}
                                            for index, (name, mime, data) in enumerate(uploads)]})
            return
        data = json.loads(raw)
        if self.path == '/api/agent' and not data:
            allowed = self.headers.get('x-api-key') == API_KEY
            self.send_json({'error': 'Either githubUrl or projectPath is required' if allowed else 'Invalid API key'},
                           400 if allowed else 401)
            return
        if self.path == '/api/projects/create-project':
            server.registered_projects.append(data['path'])
            self.send_json({'success': True})
            return
        if self.path == '/api/providers/sessions':
            server.created_sessions.append(data)
            self.send_json({'data': {'sessionId': CLOUD_SESSION}}, 201)
            return
        if self.path == '/api/auth/login':
            server.logins.append(data)
            payload = b'{"token":"fixture.web.token"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        server.requests.append((self.path, self.headers.get('x-api-key'), data))
        if server.track_history:
            server.history.append(cloud_user(data['message']))
            server.history.extend(getattr(server, 'turn_history_tail', ()))
        if server.track_running:
            server.running_since = int(time.time()*1000)
        status, payload = server.reply
        if isinstance(payload, list):
            frames = [('data: ' + json.dumps(event) + '\n\n').encode() for event in payload]
            content_type = 'text/event-stream'
        else:
            frames = [json.dumps(payload).encode()]
            content_type = 'application/json'
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(sum(map(len, frames))))
        self.end_headers()
        for index, frame in enumerate(frames):
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if getattr(server, 'pause_after_frame', None) == index:
                server.release_stream.wait(5)

    def log_message(self, *_):
        pass

    def do_DELETE(self):
        self.server.deleted_sessions.append(self.path)
        self.send_json({'success': True})


class ChatSocketFixture:
    def __init__(self, send, events=()):
        self.on_send = send
        self.events = iter(events)

    def send(self, raw):
        self.on_send(json.loads(raw))

    def recv(self):
        event = next(self.events, None)
        return json.dumps(event) if event is not None else ''

    def recv_data(self, *, control_frame):
        from websocket import ABNF
        text = self.recv()
        return (ABNF.OPCODE_TEXT, text.encode()) if text else (ABNF.OPCODE_CLOSE, b'')

    def close(self):
        pass

    def settimeout(self, _):
        pass


class CloudCLIStreamTests(unittest.TestCase):
    def test_open_sse_sends_idle_ping_without_starting_or_finishing_a_message(self):
        from unittest.mock import Mock
        handler = Mock()
        finished = threading.Event()
        with patch.object(gateway.time, 'monotonic', return_value=100) as clock:
            stream = gateway.CloudCliMessagesStream(handler, MODEL)
            clock.return_value = 120
            stream.ping_if_idle(finished)
            handler.send_sse.assert_not_called()
            stream.text('Opening text.')
            clock.return_value = 134
            stream.ping_if_idle(finished)
            self.assertEqual(handler.send_sse.call_args.args[0], 'content_block_delta')
            clock.return_value = 135
            stream.ping_if_idle(finished)
            handler.send_sse.assert_called_with('ping', {'type': 'ping'})
            finished.set()
            handler.send_sse.reset_mock()
            clock.return_value = 160
            stream.ping_if_idle(finished)
            handler.send_sse.assert_not_called()

    def test_active_stream_can_pass_old_limit(self):
        frames = [(0, {'type': 'session-id', 'sessionId': CLOUD_SESSION})]
        frames.extend((second, {'kind': 'stream_delta', 'content': 'x'}) for second in range(60, 660, 60))
        frames.extend([(625, {'kind': 'complete', 'exitCode': 0}), (626, {'type': 'done'})])
        with self.assertRaises(StopIteration) as completed:
            self.consume(frames)
        self.assertEqual(completed.exception.value[:2], (CLOUD_SESSION, [{'type': 'text', 'text': 'x' * 10}]))

    def test_heartbeats_do_not_renew_progress_and_active_stream_has_a_cap(self):
        for frames, message in (
            ([(60, None), (119, {'type': 'status', 'message': 'keepalive'}), (121, None)], 'report progress'),
            ([(second, {'kind': 'stream_delta', 'content': 'x'}) for second in range(60, 1801, 60)],
             'active runtime limit'),
        ):
            with self.subTest(message=message), self.assertRaises(gateway.CloudCliError) as failed:
                self.consume(frames)
            self.assertEqual(failed.exception.status, 504)
            self.assertIn(message, str(failed.exception))

    def test_websocket_ping_does_not_keep_an_idle_stream_alive(self):
        from websocket import ABNF
        clock = 0

        class Socket(ChatSocketFixture):
            def recv_data(self, *, control_frame):
                nonlocal clock
                self.assert_control_frames = control_frame
                clock += 30
                return ABNF.OPCODE_PING, b''

        socket = Socket(None)
        response = gateway.CloudCliSocketEvents(socket, CLOUD_SESSION)
        progress = gateway.CloudCliProgress(None, None)
        with patch.object(gateway.time, 'monotonic', side_effect=lambda: clock):
            with self.assertRaises(gateway.CloudCliError) as failed:
                next(gateway.cloudcli_events(response, progress))
        self.assertEqual(failed.exception.status, 504)
        self.assertIn('report progress', str(failed.exception))
        self.assertTrue(socket.assert_control_frames)
        self.assertEqual(clock, 120)

    def consume(self, frames):
        clock = 0

        class Source:
            def readline(self, _limit):
                nonlocal clock
                clock, event = next(frames)
                return b': heartbeat\n' if event is None else b'data: ' + json.dumps(event).encode()

        frames = iter(frames)
        progress = gateway.CloudCliProgress(None, None)
        with patch.object(gateway.time, 'monotonic', side_effect=lambda: clock):
            next(gateway.cloudcli_events(Source(), progress))


def cloud_user(message):
    return {'kind': 'text', 'role': 'user', 'sessionId': CLOUD_SESSION, 'content': message,
            'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'transcriptAnchorId': str(uuid.uuid4())}


class CloudCLIGatewayTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        self.project = self.home / 'cloud-project'
        self.project.mkdir()
        self.working_state = self.home / 'bridge'
        self.working_state.mkdir()
        self.cloud = ThreadingHTTPServer(('127.0.0.1', 0), CloudAgentFixture)
        self.cloud.requests = []
        self.cloud.logins = []
        self.cloud.model_requests = []
        self.cloud.model_options = []
        self.cloud.custom_models_only = False
        self.cloud.uploads = []
        self.cloud.created_sessions = []
        self.cloud.deleted_sessions = []
        self.cloud.registered_projects = []
        self.cloud.track_running = False
        self.cloud.track_history = False
        self.cloud.history = []
        self.cloud.running_since = None
        self.cloud.reply = (200, cloud_turn())
        self.start(self.cloud)

        self.use_patch(patch.object(gateway.Path, 'home', return_value=self.home))
        self.native_turn = self.use_patch(patch.object(gateway, 'Channel', autospec=True))
        self.native_catalog = self.use_patch(patch.object(gateway, 'native_model_catalog', autospec=True))
        self.config = {
            'port': self.cloud.server_address[1],
            'apiKey': API_KEY,
            'projectPath': str(self.project),
            'models': [MODEL],
        }
        self.config_path = self.home / gateway.CLOUDCLI_CONFIG
        self.config_path.parent.mkdir(parents=True, mode=0o700)
        self.save_config()
        self.session = str(uuid.uuid4())
        self.start_gateway()

    def use_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def start_gateway(self):
        self.manager = gateway.Manager(self.working_state, {'token': DESKTOP_TOKEN, 'instance': 'fixture'})
        self.addCleanup(self.manager.close)
        self.desktop = gateway.Server(self.manager)
        self.start(self.desktop)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, payload=None, *, path='/v1/messages', token=DESKTOP_TOKEN, session=None, diagnostic=None, beta=None):
        headers = {'Authorization': f'Bearer {token}', 'x-claude-code-session-id': session or self.session}
        if beta:
            headers['anthropic-beta'] = beta
        if diagnostic is not None:
            headers['X-SafeCode-Diagnostics'] = '1'
        with closing(http.client.HTTPConnection(*self.desktop.server_address, timeout=3)) as conn:
            if payload is None:
                conn.request('GET', path, headers=headers)
            else:
                conn.request('POST', path, body=json.dumps(payload),
                             headers={**headers, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            raw = response.read().decode()
            if diagnostic is not None and response.getheader(gateway.DIAGNOSTIC_HEADER):
                diagnostic.update(json.loads(response.getheader(gateway.DIAGNOSTIC_HEADER)))
            if response.getheader('Content-Type') == 'text/event-stream':
                data = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]
            else:
                data = json.loads(raw)
            return response.status, data

    def assertNoNativeFallback(self):
        self.native_turn.assert_not_called()
        self.native_catalog.assert_not_called()

    def test_diagnostics_distinguish_cloudcli_http_from_upstream_sse_error(self):
        self.cloud.reply = (400, {'message': 'secret must not reach the diagnostic'})
        detail = {}
        self.assertEqual(self.request(conversation('fixture'), diagnostic=detail)[0], 400)
        self.assertEqual((detail['cloudcliStatus'],detail['stage']), (400,'cloudcli_response'))
        self.assertNotIn('anthropicStatus',detail)
        progress = cloud_turn(deltas=True)[:3]
        error = 'API Error: 400 '+json.dumps(dict(type='error',request_id='req_Fixture1234',
            error=dict(type='invalid_request_error',message='private request body')))
        self.cloud.reply = (200, progress+[dict(kind='error',content=error)])
        status, events = self.request(conversation('second fixture',stream=True),diagnostic={})
        self.assertEqual(status,200)
        detail = next(event['safeCodeDiagnostic'] for event in events if event['type']=='error')
        self.assertEqual((detail['anthropicStatus'],detail['cloudcliStatus'],detail['gatewayStatus']), (400,200,400))
        self.assertEqual(detail['anthropicRequestId'],'req_Fixture1234')
        self.assertEqual(detail['anthropicError'],'invalid_request_error')
        self.assertNotIn('private',json.dumps(detail))

    def test_parallel_public_mode_allows_distinct_conversations(self):
        self.manager.cloudcli.parallel = True
        self.cloud.pause_after_frame = 0
        self.cloud.release_stream = threading.Event()
        with ThreadPoolExecutor() as pool:
            futures = [pool.submit(self.request, conversation('fixture '+str(index)), session=str(uuid.uuid4()))
                       for index in range(2)]
            try:
                deadline = time.monotonic()+2
                while len(self.cloud.requests) < 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(len(self.cloud.requests), 2)
            finally:
                self.cloud.release_stream.set()
            self.assertEqual([entry.result()[0] for entry in futures], [200, 200])
        self.assertNoNativeFallback()

    def test_messages_stream_and_desktop_context_is_not_forwarded(self):
        self.cloud.reply = (200, cloud_turn(deltas=True))
        payload = conversation('irrelevant', stream=True, thinking={'type': 'adaptive'},
                               system='private first-party system context')
        payload['messages'] = [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': '<system-reminder>private device details</system-reminder>'},
                {'type': 'text', 'text': 'Summarize the example.'},
            ]},
            {'role': 'system', 'content': 'private last system message'},
        ]
        payload['tools'] = [
            {'name': f'example_{number}', 'description': 'private Desktop tool',
             'input_schema': {'type': 'object'}}
            for number in range(114)
        ]

        status, frames = self.request(payload)

        self.assertEqual(status, 200)
        self.assertEqual(frames[0]['type'], 'message_start')
        self.assertEqual(frames[-1]['type'], 'message_stop')
        visible = ''.join(frame['delta']['text'] for frame in frames if frame['type'] == 'content_block_delta')
        self.assertIn('**SafeCode** · 已接收任务', visible)
        self.assertEqual(visible.count('Fixture reply'), 1)
        self.assertNotIn('User event', visible)
        self.assertEqual(len(self.cloud.requests), 1)
        path, key, task = self.cloud.requests[0]
        self.assertEqual((path, key), ('/api/agent', API_KEY))
        self.assertEqual(task['provider'], 'claude')
        self.assertEqual(task['projectPath'], str(self.project))
        self.assertTrue(task['message'].startswith('Summarize the example.'))
        self.assertEqual(task['model'], MODEL)
        self.assertEqual(task['effort'], 'medium')
        self.assertTrue(task['stream'])
        for forbidden in ('system', 'tools', 'max_tokens', 'thinking', 'metadata', 'githubUrl'):
            self.assertNotIn(forbidden, task)
        self.assertNotIn('private Desktop tool', json.dumps(task))
        self.assertNoNativeFallback()

    def test_live_stream_shows_intermediate_events_before_completion_and_can_resume(self):
        first_prompt = 'Run a harmless fixture command'
        self.cloud.pause_after_frame = 4
        self.cloud.release_stream = threading.Event()
        self.cloud.reply = (200, [
            {'type': 'session-id', 'sessionId': CLOUD_SESSION},
            {'kind': 'thinking', 'content': 'Reasoning excerpt from the fixture'},
            {'kind': 'tool_use', 'toolName': 'Bash', 'toolId': 'upstream-tool', 'toolInput': {'command': 'fixture command'}},
            {'kind': 'tool_result', 'toolId': 'upstream-tool', 'content': 'fixture command output'},
            {'kind': 'stream_delta', 'content': 'Fixture '},
            {'kind': 'stream_end'},
            {'kind': 'text', 'role': 'assistant', 'content': 'Fixture reply'},
            {'kind': 'complete', 'exitCode': 0}, {'type': 'done'},
        ])
        headers = {'Authorization': f'Bearer {DESKTOP_TOKEN}', 'x-claude-code-session-id': self.session}
        with closing(http.client.HTTPConnection(*self.desktop.server_address, timeout=3)) as conn:
            try:
                conn.request('POST', '/v1/messages', json.dumps(conversation(first_prompt, stream=True)), headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                decoded = []
                while True:
                    line = response.readline()
                    self.assertTrue(line)
                    if line.startswith(b'data: '):
                        decoded.append(json.loads(line[6:]))
                        if decoded[-1].get('delta', {}).get('text') == 'Fixture ':
                            break
                self.assertFalse(self.cloud.release_stream.is_set())
                self.assertNotIn('message_stop', [event['type'] for event in decoded])
                prefix = ''.join(event['delta'].get('text', '') for event in decoded if event['type'] == 'content_block_delta')
                self.assertIn('CloudCLI 正在执行云端操作', prefix)
                self.assertNotIn('fixture command', prefix)
                self.assertNotIn('Reasoning excerpt from the fixture', prefix)
                self.assertEqual([(event['index'], event['content_block']['type']) for event in decoded
                                  if event['type'] == 'content_block_start'], [(0, 'text'), (1, 'thinking'), (2, 'text')])
                thoughts = [event['delta']['thinking'] for event in decoded
                            if event.get('delta', {}).get('type') == 'thinking_delta']
                self.assertEqual(thoughts, ['Reasoning excerpt from the fixture'])
            finally:
                self.cloud.release_stream.set()
            tail = [json.loads(line[6:]) for line in response.read().splitlines() if line.startswith(b'data: ')]
        visible = prefix + ''.join(event['delta'].get('text', '') for event in tail if event['type'] == 'content_block_delta')
        self.assertEqual(tail[-1]['type'], 'message_stop')
        self.assertEqual(visible.count('Fixture reply'), 1)
        self.assertEqual(visible.count('工具调用 1 次'), 1)
        self.assertIn(CLOUD_SESSION, visible)
        self.assertEqual([event['index'] for event in decoded+tail if event['type'] == 'content_block_stop'], [0, 1, 2])
        self.assertEqual(self.request(conversation('unused', messages=[
            {'role': 'user', 'content': first_prompt}, {'role': 'assistant', 'content': visible},
            {'role': 'user', 'content': 'Follow up.'},
        ]))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

    def test_late_stream_failure_has_one_sse_error_and_is_not_dispatched_again(self):
        self.cloud.reply = (200, [
            {'type': 'session-id', 'sessionId': CLOUD_SESSION},
            {'kind': 'stream_delta', 'content': 'API '},
            {'kind': 'stream_delta', 'content': 'Error: 400 private provider detail'},
            {'kind': 'text', 'role': 'assistant', 'content': 'API Error: 400 private provider detail'},
        ])
        payload = conversation('A failing tool run.', stream=True)
        status, events = self.request(payload)
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('HTTP 400', events[-1]['error']['message'])
        self.assertNotIn('private provider detail', json.dumps(events))
        self.assertNotIn('message_stop', [event['type'] for event in events])
        self.assertEqual(self.request(payload)[0], 409)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_original_cloud_session_resumes_and_usage_is_returned(self):
        first = conversation('First task')
        status, response = self.request(first)
        self.assertEqual(status, 200)
        self.assertEqual(response['content'], [{'type': 'text', 'text': 'Fixture reply'}])
        self.assertEqual(response['usage']['input_tokens'], 100)
        self.assertEqual(response['usage']['output_tokens'], 20)
        self.assertEqual(response['usage']['cache_read_input_tokens'], 17)
        self.assertEqual(response['usage']['cache_creation_input_tokens'], 8)

        next_payload = conversation('irrelevant', messages=[
            {'role': 'user', 'content': 'First task'},
            {'role': 'assistant', 'content': response['content']},
            {'role': 'user', 'content': 'Follow-up task'},
        ])
        self.cloud.reply = (200, cloud_turn('Follow-up reply'))
        status, follow_up = self.request(next_payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(next_payload), (200, follow_up))
        first_task, second_task = [record[2] for record in self.cloud.requests]
        self.assertNotIn('sessionId', first_task)
        self.assertEqual(second_task['sessionId'], CLOUD_SESSION)
        self.assertEqual(second_task['message'], 'Follow-up task')
        self.assertNoNativeFallback()

    def test_directory_groups_independent_chats_and_keeps_same_named_folders_distinct(self):
        directories = ['/Users/example/Work', '/Users/example/Work', '/Users/example/Other/Work']
        for number, directory in enumerate(directories):
            self.session = str(uuid.uuid4())
            self.cloud.reply = (200, cloud_turn(session_id=f'cloudcli-fixture-session-{number}'))
            payload = conversation('Same opening text.', system=desktop_environment(directory))
            self.assertEqual(self.request(payload)[0], 200)

        tasks = [entry[2] for entry in self.cloud.requests]
        self.assertEqual(len(tasks), 3)
        self.assertTrue(all('sessionId' not in task for task in tasks))
        projects = [Path(task['projectPath']) for task in tasks]
        self.assertTrue(all(project.is_dir() and project.is_relative_to(self.home) for project in projects))
        self.assertEqual([project.name for project in projects], ['Work', 'Work', 'Work'])
        self.assertEqual(projects[0], projects[1])
        self.assertNotEqual(projects[0], projects[2])
        self.assertNotEqual(projects[0], self.project)
        self.assertNoNativeFallback()

    def test_same_named_desktop_folders_are_isolated_by_paired_device(self):
        port = self.cloud.server_address[1]
        cfg = gateway.cloudcli_config({**self.config, 'port': port})
        projects = []
        for identifier in ('1'*20, '2'*20):
            device = {'id': identifier, 'platform': 'windows', 'hostname': 'Windows',
                      'alias': 'win-'+identifier, 'command': 'fixture-ssh-'+identifier}
            backend = gateway.CloudCliBackend(self.working_state, cfg, device)
            directory = gateway.PureWindowsPath(r'C:\Users\Example\Desktop')
            project, workspace = backend.project_for_directory(directory)
            self.assertEqual(workspace, str(directory))
            self.assertTrue(Path(project).name.startswith('Desktop · win-'))
            backend.prepare_project(project, workspace)
            self.assertEqual(backend.project_for_directory(gateway.PureWindowsPath(str(directory).lower()))[0], project)
            self.assertIn(device['command'], backend.host_instruction(project, workspace))
            self.assertFalse((Path(project)/'.mcp.json').exists())
            projects.append(project)
        self.assertNotEqual(*projects)

    def test_windows_project_reuses_original_name_when_path_case_changes(self):
        for directory in (r'C:\Users\Example\Work', r'c:\users\example\WORK'):
            self.session = str(uuid.uuid4())
            payload = conversation('Open a project chat.', system=desktop_environment(directory))
            self.assertEqual(self.request(payload)[0], 200)

        first, second = [Path(entry[2]['projectPath']) for entry in self.cloud.requests]
        self.assertEqual(first.name, 'Work')
        self.assertEqual(first, second)
        self.assertNoNativeFallback()

    def test_grouped_chat_keeps_its_project_when_resumed_after_gateway_restart(self):
        first = conversation('First task', system=desktop_environment('/Users/example/First'))
        status, response = self.request(first)
        self.assertEqual(status, 200)
        project = Path(self.cloud.requests[0][2]['projectPath'])
        self.assertEqual(project.name, 'First')

        self.start_gateway()
        continuation = [*first['messages'], {'role': 'assistant', 'content': response['content']},
                        {'role': 'user', 'content': 'Follow up'}]
        self.cloud.reply = (200, cloud_turn('Continued'))
        follow_up = conversation('unused', messages=continuation,
                                 system=desktop_environment('/Users/example/Different'))
        self.assertEqual(self.request(follow_up)[0], 200)
        self.assertEqual(len(self.cloud.requests), 2)
        resumed_task = self.cloud.requests[1][2]
        self.assertEqual(resumed_task['sessionId'], CLOUD_SESSION)
        self.assertEqual(resumed_task['projectPath'], str(project))
        self.assertNoNativeFallback()

    def test_upgraded_gateway_resumes_preexisting_session_in_original_project(self):
        history = [{'role': 'user', 'content': 'Earlier task'},
                   {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Earlier reply'}]}]
        old_identity = {**self.config, 'hostControl': None,
                        'apiKey': hashlib.sha256(API_KEY.encode()).hexdigest()}
        namespace = hashlib.sha256(json.dumps(old_identity, sort_keys=True).encode()).hexdigest() + ':' + self.session
        self.working_state = self.home / 'older-bridge'
        self.working_state.mkdir()
        with closing(sqlite3.connect(self.working_state/'cloudcli-history.sqlite3')) as db, db:
            db.execute('CREATE TABLE sessions (scope TEXT NOT NULL, history TEXT NOT NULL, '
                       'session TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (scope, history))')
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (
                namespace, gateway.history_signature(history, 'cloudcli'), CLOUD_SESSION, time.time(),
            ))

        self.start_gateway()
        messages = history + [{'role': 'user', 'content': 'Later task'}]
        follow_up = conversation('unused', messages=messages,
                                 system=desktop_environment('/Users/example/New'))
        self.assertEqual(self.request(follow_up)[0], 200)
        self.assertEqual(len(self.cloud.requests), 1)
        resumed_task = self.cloud.requests[0][2]
        self.assertEqual(resumed_task['sessionId'], CLOUD_SESSION)
        self.assertEqual(resumed_task['projectPath'], str(self.project))
        self.assertNoNativeFallback()

    def test_models_and_auth_do_not_contact_cloud_or_native_cli(self):
        status, listing = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in listing['data']], [MODEL])
        self.assertEqual(self.request(conversation('fixture'), token='wrong')[0], 401)
        self.assertFalse(self.cloud.requests)
        self.assertFalse(self.cloud.model_requests)
        self.assertFalse(self.cloud.logins)
        self.assertNoNativeFallback()

    def test_catalog_only_advertises_verified_effort_for_an_allowed_model(self):
        self.config['desktopQuestions'] = {'username': 'fixture-user', 'password': 'fixture-password'}
        self.cloud.model_options = [
            {'value': 'haiku', 'label': 'Alias should not be assigned', 'effort': {'values': [{'value': 'max'}]}},
            {'value': MODEL, 'label': 'Fixture Haiku', 'effort': {'values': [
                {'value': 'medium'}, {'value': 'low'}, {'value': 'ultracode'}, {'value': 'medium'},
            ]}},
            {'value': 'claude-fable-5-1', 'label': 'Outside the gateway allowlist'},
        ]
        self.save_config()
        self.start_gateway()
        status, models = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual(len(models['data']), 1)
        self.assertEqual(models['data'][0]['display_name'], 'Fixture Haiku')
        self.assertEqual(models['data'][0]['supported_effort_levels'], ['low', 'medium', 'ultracode'])
        self.assertEqual(self.request(path='/v1/models'), (200, models))
        self.assertFalse(self.cloud.requests)
        self.assertEqual(self.cloud.model_requests, ['Bearer fixture.web.token'])
        self.assertNoNativeFallback()

    def use_automatic_models(self, *, web_catalog=False):
        project = self.home / gateway.DEFAULT_CLOUDCLI_PROJECT
        project.mkdir(parents=True, exist_ok=True)
        self.config.update(projectPath=str(project), models=list(gateway.DEFAULT_CLOUDCLI_MODELS))
        if web_catalog:
            self.config['desktopQuestions'] = {'username': 'fixture-user', 'password': 'fixture-password'}
        self.save_config()
        self.start_gateway()
        self.use_patch(patch.object(gateway, '_native_catalog_expires', 0.0))
        self.use_patch(patch.object(gateway, '_native_catalog_options', {}))

    def test_automatic_agent_key_gateway_discovers_native_models_and_selectors(self):
        self.use_automatic_models()
        opus, fable = 'claude-opus-5-5', 'claude-fable-5-1'
        self.native_catalog.return_value = gateway.model_catalog([
            {'value': 'default', 'displayName': 'Default (recommended)'},
            {'value': 'best', 'displayName': 'Best available'},
            {'value': 'opusplan', 'displayName': 'Opus Plan'},
            {'value': 'fable', 'resolvedModel': fable, 'displayName': 'Fable',
             'supportedEffortLevels': ['low', 'high', 'max']},
            {'value': 'opus[1m]', 'resolvedModel': opus+'[1m]', 'displayName': 'Opus (1M context)'},
            {'value': 'opus', 'resolvedModel': opus, 'displayName': 'Opus', 'supportedEffortLevels': ['high', 'xhigh']},
        ], self.home, include_options=True)

        status, catalog = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        models = {row['id']: row for row in catalog['data']}
        self.assertEqual(list(models)[:3], ['default', 'best', 'opusplan'])
        self.assertTrue(models['opus']['supports_1m'])
        self.assertTrue(models[opus]['supports_1m'])
        self.assertEqual(models['fable']['behaves_as'], fable)
        self.assertEqual(models['fable']['supported_effort_levels'], ['low', 'high', 'max'])
        self.assertIn(fable, models)
        self.assertEqual(self.manager.cloudcli.resolve_model(fable)[0], 'fable')
        self.assertEqual(self.manager.cloudcli.resolve_model(opus, True)[0], 'opus[1m]')
        self.assertEqual(self.request(conversation('Fixture', model='opusplan'))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['model'], 'opusplan')
        self.native_catalog.assert_called_once()
        self.native_turn.assert_not_called()

        self.native_catalog.return_value = {'data': [{'id': opus}]}
        with patch.object(gateway, '_native_catalog_expires', 0.0):
            status, catalog = self.request(path='/v1/models')
            self.assertEqual(status, 200)
            self.assertEqual([row['id'] for row in catalog['data']], [opus])
            self.assertEqual(self.request(conversation('Unavailable fixture', model=fable))[0], 404)

    def test_automatic_gateway_mirrors_cloudcli_choices_and_effort(self):
        self.use_automatic_models(web_catalog=True)
        choices = [
            ('default', 'Default (recommended)'), ('best', 'Best available'), ('fable', 'Fable 5'),
            ('sonnet', 'Sonnet'), ('sonnet[1m]', 'Sonnet (1M context)'),
            ('opus', 'Opus'), ('opus[1m]', 'Opus (1M context)'),
            ('haiku', 'Haiku'), ('opusplan', 'Opus Plan'), ('vendor/custom.model', 'Custom Model'),
        ]
        efforts = [{'value': effort} for effort in ('low', 'medium', 'high', 'xhigh', 'max', 'ultracode')]
        self.cloud.model_options = [{'value': model, 'label': label, 'effort': {'values': efforts}}
                                    for model, label in choices]
        self.native_catalog.return_value = {'data': []}

        status, catalog = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        models = {row['id']: row for row in catalog['data']}
        self.assertEqual(list(models)[:8], ['default','best','fable','sonnet','opus','haiku','opusplan','vendor/custom.model'])
        self.assertTrue(models['opus']['supports_1m'])
        self.assertTrue(models['sonnet']['supports_1m'])
        self.assertEqual(models['opus']['supported_effort_levels'], [entry['value'] for entry in efforts])
        self.assertEqual(models['best']['desktop_model_id'], 'safecode-cloudcli-best')
        self.assertEqual(models['opus']['desktop_ultracode_model_id'], 'safecode-ultracode/opus')
        self.assertTrue(models['opus']['ultracode_supports_1m'])

        for model, beta, effort, expected, forwarded_effort in (
                ('safecode-cloudcli-default', None, 'default', 'default', None),
                ('safecode-cloudcli-best', None, 'default', 'best', None),
                ('safecode-cloudcli-opusplan', None, 'medium', 'opusplan', 'medium'),
                ('safecode-ultracode/opus', 'context-1m-2025-08-07', 'xhigh', 'opus[1m]', 'ultracode'),
                ('opus', None, 'ultracode', 'opus', 'ultracode'),
                ('safecode-cloudcli-sonnet[1m]', None, 'max', 'sonnet[1m]', 'max'),
                ('vendor/custom.model', None, 'high', 'vendor/custom.model', 'high')):
            with self.subTest(model=model):
                status, _ = self.request(conversation('Fixture', model=model, output_config={'effort':effort}),
                                         session=str(uuid.uuid4()), beta=beta)
                self.assertEqual(status, 200)
                task = self.cloud.requests[-1][2]
                self.assertEqual(task['model'], expected)
                self.assertEqual(task.get('effort'), forwarded_effort)
        self.native_turn.assert_not_called()
        self.assertEqual(self.cloud.model_requests, ['Bearer fixture.web.token'])

    def test_custom_only_cloudcli_list_does_not_expose_native_choices(self):
        self.use_automatic_models(web_catalog=True)
        self.cloud.custom_models_only = True
        self.cloud.model_options = [{'value': 'company/only-model[1m]', 'label': 'Company 1M'}]
        status, catalog = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual([row['id'] for row in catalog['data']], ['company/only-model[1m]'])
        self.assertEqual(self.request(conversation('Unavailable', model='claude-opus-5-5'))[0], 404)
        self.assertEqual(self.request(conversation('Unsupported effort', model='company/only-model',
            output_config={'effort':'ultracode'}))[0], 400)
        self.assertEqual(self.request(conversation('Allowed', model='company/only-model'))[0], 200)
        self.assertEqual([request[2]['model'] for request in self.cloud.requests], ['company/only-model[1m]'])
        self.assertNoNativeFallback()

    def test_extended_context_is_opt_in_and_keeps_existing_session_routing(self):
        opus = 'claude-opus-5-5'
        self.config['models'].append(opus)
        self.save_config()
        self.start_gateway()
        beta = 'context-1m-2025-08-07'
        self.assertEqual(self.request(conversation('Attempt 1M before enable', model=opus), beta=beta)[0], 400)
        self.assertFalse(self.cloud.requests)
        first = conversation('Start a conversation', model=opus)
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        identity = self.manager.cloudcli.identity
        self.config['extendedContextModels'] = [opus]
        self.save_config()
        self.start_gateway()
        self.assertEqual(self.manager.cloudcli.identity, identity)
        status, models = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual([(row['id'], row['supports_1m']) for row in models['data']], [(MODEL, False), (opus, True)])
        follow_up = conversation('unused', model=opus, messages=[
            *first['messages'], {'role': 'assistant', 'content': reply['content']},
            {'role': 'user', 'content': 'Continue with the extended window.'},
        ])
        self.assertEqual(self.request(follow_up, beta=beta)[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        self.assertEqual(self.cloud.requests[-1][2]['model'], opus+'[1m]')
        self.assertEqual(self.request(conversation('Alternate extended request', model=opus+'[1m]'),
                                      session=str(uuid.uuid4()))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['model'], opus+'[1m]')
        self.assertEqual(self.request(conversation('Disallowed variant', model=MODEL+'[1m]'))[0], 400)
        self.assertEqual(len(self.cloud.requests), 3)
        self.assertNoNativeFallback()

    def test_cloudcli_reported_model_difference_is_visible(self):
        frames = cloud_turn()
        for event in frames:
            if event.get('role') == 'assistant':
                event['model'] = 'claude-sonnet-4-6'
        self.cloud.reply = (200, frames)
        status, response = self.request(conversation('Report the selected model.'))
        self.assertEqual(status, 200)
        self.assertIn('CloudCLI 报告本轮使用了其他模型', response['content'][0]['text'])
        self.assertIn('claude-sonnet-4-6', response['content'][0]['text'])

    def test_cloudcli_api_rejection_preserves_status_without_raw_detail(self):
        self.cloud.reply = (403, {'error': 'private cloud provider diagnostic'})
        status, response = self.request(conversation('fixture'))
        self.assertEqual(status, 403)
        self.assertNotIn('private cloud provider diagnostic', json.dumps(response))
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertNoNativeFallback()

    def test_sse_error_or_disconnect_never_reruns_the_same_task(self):
        started = {'type': 'session-id', 'sessionId': CLOUD_SESSION}
        assistant = {'kind': 'text', 'role': 'assistant', 'content': 'Partial reply'}
        for suffix, result in [
            ('error', [{'kind': 'error', 'content': 'private detailed error'}]),
            ('interrupted stream', [assistant]),
        ]:
            with self.subTest(suffix=suffix):
                count_before = len(self.cloud.requests)
                self.cloud.reply = (200, [started, *result])
                payload = conversation(f'Run the fixture task once, case {suffix}.')
                status, response = self.request(payload)
                self.assertEqual(status, 502)
                self.assertNotIn('private detailed error', json.dumps(response))
                self.assertEqual(self.request(payload)[0], 409)
                self.assertEqual(len(self.cloud.requests), count_before + 1)
        self.assertNoNativeFallback()

    def test_interrupted_turn_can_be_edited_without_replaying_it(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        first = conversation('First confirmed turn.')
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        history = [*first['messages'], {'role': 'assistant', 'content': reply['content']}]
        broken = conversation('unused', stream=True, messages=[*history, {'role': 'user', 'content': 'Broken turn.'}])
        self.cloud.reply = (200, [{'type': 'session-id', 'sessionId': CLOUD_SESSION},
                                  {'kind': 'stream_delta', 'content': 'Partial reply.'}])
        self.cloud.turn_history_tail = [{'kind': 'tool_use', 'content': 'fixture'}] * 130
        status, interrupted = self.request(broken)
        self.assertEqual(status, 200)
        self.assertEqual(interrupted[-1]['type'], 'error')
        failed_anchor = [row for row in self.cloud.history if row.get('role') == 'user'][-1]['transcriptAnchorId']
        self.assertEqual(self.request(broken)[0], 409)
        self.assertEqual(len(self.cloud.requests), 2)

        actions = []
        events = [{'kind': 'text', 'role': 'assistant', 'sessionId': CLOUD_SESSION, 'content': 'Edited reply.'},
                  {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}]
        replacement = {'role': 'user', 'content': 'Explicit replacement for broken turn.'}
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *args, **kwargs: ChatSocketFixture(actions.append, events), OSError)):
            status, response = self.request(conversation('unused', messages=[*history, replacement]))
        self.assertEqual(status, 200)
        self.assertIn('Edited reply.', response['content'][0]['text'])
        action, = actions
        self.assertEqual((action['type'], action['sessionId'], action['anchorId'], action['content']),
                         ('chat.edit-send', CLOUD_SESSION, failed_anchor, replacement['content']))
        self.assertEqual(len(self.cloud.requests), 2)

    def test_old_interrupted_checkpoint_recovers_only_its_proven_edit(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        first = conversation('First confirmed turn.')
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        prefix = [*first['messages'], {'role': 'assistant', 'content': reply['content']}]
        broken = conversation('unused', stream=True, messages=[*prefix, {'role': 'user', 'content': 'Previous task.'}])
        self.cloud.reply = (200, [{'type': 'session-id', 'sessionId': CLOUD_SESSION},
                                  {'kind': 'stream_delta', 'content': 'Partial reply.'}])
        self.assertEqual(self.request(broken)[1][-1]['type'], 'error')
        anchor = self.cloud.history[-1]['transcriptAnchorId']
        self.cloud.history.extend([{'kind': 'thinking', 'content': 'fixture'}] * 80)
        with closing(sqlite3.connect(self.manager.cloudcli.path)) as db, db:
            db.execute('UPDATE session_state SET tip_anchor=NULL WHERE session=?', (CLOUD_SESSION,))
            db.execute('DELETE FROM edit_turns WHERE anchor=?', (anchor,))
        self.assertEqual(self.request(broken)[0], 409)
        actions = []
        events = [{'kind': 'text', 'role': 'assistant', 'sessionId': CLOUD_SESSION, 'content': 'Edited reply.'},
                  {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}]
        replacement = {'role': 'user', 'content': 'Replacement task.'}
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *args, **kwargs: ChatSocketFixture(actions.append, events), OSError)):
            self.assertEqual(self.request(conversation('unused', messages=[*prefix[:-1], replacement]))[0], 409)
            self.assertFalse(actions)
            self.assertEqual(self.request(conversation('unused', messages=[*prefix, replacement]))[0], 200)
        action, = actions
        self.assertEqual((action['type'], action['sessionId'], action['anchorId']),
                         ('chat.edit-send', CLOUD_SESSION, anchor))

    def test_desktop_compaction_resumes_only_the_same_session_with_its_confirmed_summary(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        summary = 'Remember SC214-COMPACT and the original work item.'
        self.cloud.reply = (200, cloud_turn('<analysis>Fixture</analysis>\n<summary>\n'+summary+'\n</summary>'))
        status, _ = self.request(conversation('Produce a summary for the Desktop compact command.'))
        self.assertEqual(status, 200)
        wrapper = (gateway.DESKTOP_COMPACT_PREFIX+'\n\n'
                   '<system-reminder>\n<tools-used-before-summary>Read</tools-used-before-summary>\n</system-reminder>\n\n'
                   '> **SafeCode** · 已接收任务\n\n'
                   'Summary:\n')
        context = {'role': 'user', 'content': wrapper+summary+'\n\nContinue the conversation.'}
        previous = {'role': 'assistant', 'content': 'Continue from the same task.'}
        latest = {'role': 'user', 'content': '<local-command-caveat>This ran in Claude Code.</local-command-caveat>\n'
                  '<command-name>/compact</command-name>\n<command-message>compact</command-message>\n'
                  '<command-args>Keep the marker.</command-args>\n'
                  '<local-command-stdout>Compacted</local-command-stdout>\nGive the original marker.'}
        good = conversation('unused', messages=[context, previous, latest])
        wrong = conversation('unused', messages=[{**context, 'content': wrapper+'Forged summary.'}, previous, latest])
        self.assertEqual(self.request(wrong)[0], 409)
        self.assertEqual(self.request(good, session=str(uuid.uuid4()))[0], 409)
        self.assertEqual(len(self.cloud.requests), 1)
        self.cloud.reply = (200, cloud_turn('SC214-COMPACT'))
        status, result = self.request(good)
        self.assertEqual(status, 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        self.assertEqual(self.cloud.requests[-1][2]['message'], 'Give the original marker.')
        later = [*[context, previous, latest], {'role': 'assistant', 'content': result['content']},
                 {'role': 'user', 'content': 'Continue after the compact checkpoint.'}]
        self.assertEqual(self.request(conversation('unused', messages=later))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

    def test_interrupted_turn_can_continue_with_its_visible_partial_reply(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        broken = conversation('Interrupted first task.', stream=True)
        self.cloud.reply = (200, [{'type': 'session-id', 'sessionId': CLOUD_SESSION},
                                  {'kind': 'stream_delta', 'content': 'Partial reply.'}])
        status, events = self.request(broken)
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]['type'], 'error')
        stream = gateway.StreamMessage()
        for event in events[:-1]:
            stream.feed(event)
        self.cloud.reply = (200, cloud_turn('Continued after interruption.'))
        messages = [*broken['messages'], {'role': 'assistant', 'content': stream.partial()},
                    {'role': 'user', 'content': 'Continue the task without repeating the last action.'}]
        status, response = self.request(conversation('unused', messages=messages))
        self.assertEqual(status, 200)
        self.assertIn('Continued after interruption.', response['content'][0]['text'])
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

    def test_upstream_error_is_not_treated_as_assistant_text(self):
        self.cloud.reply = (200, cloud_turn('API Error: 400 private provider detail'))
        status, response = self.request(conversation('Return the answer'))
        self.assertEqual(status, 400)
        self.assertNotIn('private provider detail', json.dumps(response))
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertNoNativeFallback()

    def test_private_config_is_required_before_starting(self):
        self.config_path.chmod(0o644)
        with self.assertRaises(gateway.GatewayError) as rejected:
            gateway.Manager(self.working_state, {'token': DESKTOP_TOKEN, 'instance': 'fixture'})
        self.assertEqual(rejected.exception.status, 503)
        self.assertNotIn(API_KEY, str(rejected.exception))
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_workspace_target_requires_ssh_binding_and_cloudcli_mcp_registration(self):
        workspace = '/Users/example/Work'
        self.config['hostControl'] = {'clientId': CLIENT_ID, 'workspace': workspace, 'deviceName': 'Example Mac'}
        self.save_config()
        self.start_gateway()
        payload = conversation('List the example files.', system=desktop_environment(workspace),
                               projectPath='/client/supplied/path',
                               hostControl={'workspace': '/client/supplied/path'})
        self.assertEqual(self.request(payload)[0], 409)
        self.assertFalse(self.cloud.requests)

        ssh = self.home / '.ssh/safe-code-host' / CLIENT_ID / 'config'
        ssh.parent.mkdir(parents=True, mode=0o700)
        ssh.write_text('# Safe Code host bridge ' + CLIENT_ID + '\n')
        ssh.chmod(0o600)
        self.assertEqual(self.request(payload)[0], 409)
        self.assertFalse(self.cloud.requests)

        bridge = self.home / 'host_mcp_fixture.py'
        bridge.touch()
        self.use_patch(patch.object(gateway, 'CLOUDCLI_MCP_SCRIPT', str(bridge)))
        mcp_servers = {
            'safecode_host': {
                'type': 'stdio',
                'command': '/usr/bin/python3',
                'args': ['-I', '-B', str(bridge), '--client-id', CLIENT_ID, '--workspace', workspace],
            },
        }
        native_config = self.home / '.claude.json'
        native_config.write_text(json.dumps({
            'claudeProjects': {str(self.project): {'mcpServers': mcp_servers}},
        }))
        native_config.chmod(0o600)

        self.assertEqual(self.request(payload)[0], 200)
        self.assertEqual(len(self.cloud.requests), 1)
        task = self.cloud.requests[0][2]
        self.assertEqual(task['projectPath'], str(self.project))
        self.assertIn(workspace, task['message'])
        self.assertIn('Example Mac', task['message'])
        self.assertIn('List the example files.', task['message'])
        self.assertNotIn('/client/supplied/path', json.dumps(task))
        self.assertNotIn(CLIENT_ID, json.dumps(task))
        self.assertNoNativeFallback()

        # A sibling is a distinct group and cannot inherit this Mac connection's workspace.
        self.session = str(uuid.uuid4())
        outside = conversation('Different project.', system=desktop_environment('/Users/example/Work-Other'))
        self.assertEqual(self.request(outside)[0], 200)
        task = self.cloud.requests[1][2]
        self.assertEqual(Path(task['projectPath']).name, 'Work-Other')
        self.assertNotIn(workspace, task['message'])
        self.assertNotIn(task['projectPath'], json.loads(native_config.read_text())['claudeProjects'])
        self.assertFalse((Path(task['projectPath']) / '.mcp.json').exists())

        self.session = str(uuid.uuid4())
        child = workspace + '/Notes'
        self.assertEqual(self.request(conversation('Child project.', system=desktop_environment(child)))[0], 200)
        task = self.cloud.requests[2][2]
        self.assertEqual(Path(task['projectPath']).name, 'Notes')
        self.assertIn(child, task['message'])
        settings_file = Path(task['projectPath']) / '.mcp.json'
        self.assertEqual(json.loads(settings_file.read_text()), {'mcpServers': {'safecode_host': {
            'type': 'stdio',
            'command': '/usr/bin/python3',
            'args': ['-I', '-B', str(bridge), '--client-id', CLIENT_ID, '--workspace', child, '--managed-project'],
        }}})

    def test_adaptive_workspaces_use_the_selected_folder_and_drop_legacy_fallback(self):
        desktop = '/Users/example/Desktop'
        work = '/Users/example/Work'
        test_folder = '/Users/example/cloudcli-test'
        self.config['hostControl'] = {'clientId': CLIENT_ID, 'workspace': test_folder}
        self.save_config()
        self.start_gateway()
        old_namespace = self.manager.cloudcli.identity
        self.config['hostControl']['projectWorkspaces'] = [desktop, work]
        self.save_config()
        bridge = self.home/'host_mcp_fixture.py'
        bridge.touch()
        self.use_patch(patch.object(gateway, 'CLOUDCLI_MCP_SCRIPT', str(bridge)))
        ssh = self.home/'.ssh/safe-code-host'/CLIENT_ID/'config'
        ssh.parent.mkdir(parents=True)
        ssh.write_text('# Safe Code host bridge '+CLIENT_ID+'\n')
        self.start_gateway()
        self.assertEqual(self.manager.cloudcli.identity, old_namespace)
        entry = self.manager.cloudcli.host_tool_entry(desktop)
        entry['args'].append('--managed-project')
        (self.home/'.claude.json').write_text(json.dumps({
            'claudeProjects': {str(self.project): {'mcpServers': {'safecode_host': entry}}},
        }))
        for number, (cwd, expected) in enumerate(((desktop, desktop), (work+'/Notes', work+'/Notes'),
                                                   (test_folder, None), (None, None), ('/vm/mount/Desktop', desktop))):
            self.session = str(uuid.uuid4())
            self.cloud.reply = (200, cloud_turn(session_id=f'cloudcli-adaptive-{number}'))
            payload = conversation('Fixture task')
            if cwd is not None:
                payload['system'] = desktop_environment(cwd)
            resolved = {'workspace': desktop} if cwd == '/vm/mount/Desktop' else {}
            with patch.object(gateway.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(resolved).encode())):
                status, response = self.request(payload)
            self.assertEqual(status, 200)
            task = self.cloud.requests[-1][2]
            self.assertNotIn(test_folder, task['message'])
            if expected:
                self.assertIn(f'当前工作目录为 `{expected}`', task['message'])
            else:
                self.assertNotIn('当前工作目录为', task['message'])
                self.assertNotEqual(task['projectPath'], str(self.project))
                self.assertFalse((Path(task['projectPath'])/'.mcp.json').exists())
            if cwd == desktop:
                self.assertEqual(task['projectPath'], str(self.project))
                messages = [*payload['messages'], {'role': 'assistant', 'content': response['content']},
                            {'role': 'user', 'content': 'Follow up'}]
                self.assertEqual(self.request(conversation('unused', messages=messages))[0], 200)
                self.assertEqual(self.cloud.requests[-1][2]['message'], 'Follow up')

    def test_ssh_prompt_uses_the_private_bridge_alias_for_an_authorized_project(self):
        workspace = '/Users/example/Desktop'
        self.config['hostControl'] = {'clientId': CLIENT_ID, 'workspace': workspace, 'deviceName': 'Example Mac',
                                      'projectWorkspaces': [workspace], 'allowSshShell': True}
        ssh_config = self.home/'.ssh/safe-code-host'/CLIENT_ID/'config'
        ssh_config.parent.mkdir(parents=True)
        ssh_config.write_text('# Safe Code host bridge '+CLIENT_ID+'\nHost desk-mac\n')
        ssh_config.chmod(0o600)
        bridge = self.home/'host_mcp_fixture.py'
        bridge.touch()
        self.use_patch(patch.object(gateway, 'CLOUDCLI_MCP_SCRIPT', str(bridge)))
        self.save_config()
        self.start_gateway()
        (self.home/'.claude.json').write_text('{}')
        payload = conversation('Identify this machine.', system=desktop_environment(workspace))
        status, reply = self.request(payload)
        self.assertEqual(status, 200)
        prompt = self.cloud.requests[-1][2]['message']
        self.assertIn(f'`ssh -F {ssh_config} desk-mac`', prompt)
        self.assertIn(f'本机是 Example Mac（`macOS`），当前工作目录为 `{workspace}`。', prompt)
        self.assertIn('每条命令从项目根目录开始', prompt)
        self.assertNotIn('本机文件工具', prompt)
        self.assertTrue(prompt.startswith('Identify this machine.\n\n本机是'))
        self.assertNotIn('SafeCode workspace context:', prompt)
        self.assertNotIn('VPS', prompt)
        self.start_gateway()
        history = [*payload['messages'], {'role': 'assistant', 'content': reply['content']},
                   {'role': 'user', 'content': 'Continue in the same folder.'}]
        status, reply = self.request(conversation('unused', messages=history, system=payload['system']))
        self.assertEqual(status, 200)
        self.assertEqual(self.cloud.requests[-1][2]['message'], 'Continue in the same folder.')
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

        # A conversation saved before the marker existed receives the new prompt once.
        with closing(sqlite3.connect(self.manager.cloudcli.path)) as db, db:
            db.execute('DROP TABLE workspace_prompts')
        self.start_gateway()
        history += [{'role': 'assistant', 'content': reply['content']}, {'role': 'user', 'content': 'Resume after upgrade.'}]
        status, reply = self.request(conversation('unused', messages=history, system=payload['system']))
        self.assertEqual(status, 200)
        resumed = self.cloud.requests[-1][2]
        self.assertEqual(resumed['sessionId'], CLOUD_SESSION)
        self.assertIn(f'`ssh -F {ssh_config} desk-mac`', resumed['message'])
        history += [{'role': 'assistant', 'content': reply['content']}, {'role': 'user', 'content': 'Another follow-up.'}]
        self.assertEqual(self.request(conversation('unused', messages=history, system=payload['system']))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['message'], 'Another follow-up.')

        self.session = str(uuid.uuid4())
        child = workspace+'/Notes'
        backend = self.manager.cloudcli
        child_project, _ = backend.project_for_directory(gateway.PurePosixPath(child))
        backend.private_project_directory(Path(child_project))
        entry = backend.host_tool_entry(child)
        entry['args'].append('--managed-project')
        settings = Path(child_project)/'.mcp.json'
        backend.project_settings(settings, {'mcpServers': {'safecode_host': entry}})
        self.assertEqual(self.request(conversation('Example.', system=desktop_environment(child)))[0], 200)
        self.assertFalse(settings.exists())
        self.assertIn('ssh -F', self.cloud.requests[-1][2]['message'])
        self.assertNotIn('sessionId', self.cloud.requests[-1][2])
        self.session = str(uuid.uuid4())
        outside = conversation('Example.', system=desktop_environment('/Users/example/Other'))
        self.assertEqual(self.request(outside)[0], 200)
        self.assertNotIn('ssh -F', self.cloud.requests[-1][2]['message'])
        self.session = str(uuid.uuid4())
        ssh_config.write_text('# Safe Code host bridge '+CLIENT_ID+'\nHost desk-mac other\n')
        attempts = len(self.cloud.requests)
        self.assertEqual(self.request(payload)[0], 409)
        self.assertEqual(len(self.cloud.requests), attempts)
        self.assertNoNativeFallback()

    def test_public_cloudcli_backend_rejects_unrestricted_direct_listeners(self):
        with patch.object(public_gateway, 'load_cloudcli_config', return_value={**self.config,'hostControl':{'clientId':CLIENT_ID}}):
            with self.assertRaisesRegex(ValueError, 'shared workstation'):
                public_gateway.PublicServer(self.working_state, {'bind': '0.0.0.0', 'port': 0})
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_deployment_verifies_agent_credentials_without_dispatching_a_turn(self):
        gateway.verify_cloudcli_agent(self.config)
        with self.assertRaisesRegex(gateway.GatewayError, '拒绝了'):
            gateway.verify_cloudcli_agent({**self.config, 'apiKey': 'invalid-test-key'})
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_existing_cloudcli_account_accepts_only_agent_key(self):
        self.config_path.unlink()
        self.use_patch(patch.object(setup_cloudcli, 'cloudcli_needs_setup', return_value=False))
        verify = self.use_patch(patch.object(setup_cloudcli, 'verify_cloudcli_agent',
            side_effect=lambda config: gateway.verify_cloudcli_agent({**config,'port':self.config['port']})))
        login = self.use_patch(patch.object(setup_cloudcli, 'cloudcli_web_login'))
        state = self.working_state/'config.json'
        listener = {'bind': '0.0.0.0', 'port': 45144}
        state.write_text(json.dumps(listener))
        self.assertEqual(setup_cloudcli.prepare_public_gateway(self.home, self.working_state, {}),
                         {'cloudcliKeyRequired':True})
        self.assertFalse(self.config_path.exists())
        result = setup_cloudcli.prepare_public_gateway(self.home, self.working_state, {'apiKey':API_KEY})
        self.assertEqual(result['bind'], listener['bind'])
        self.assertIsNone(result['cloudcliAccount'])
        self.assertEqual(hashlib.sha256(self.config_path.read_bytes()).hexdigest(), result['created'])
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(gateway.load_cloudcli_config()['desktopToolApprovals'])
        self.assertEqual(json.loads(state.read_text()), listener)
        verify.assert_called_once()
        login.assert_not_called()
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_first_cloudcli_setup_registers_once_and_recovers_pending_credentials(self):
        self.config_path.unlink()
        needs_setup = self.use_patch(patch.object(setup_cloudcli, 'cloudcli_needs_setup', side_effect=[True, False]))
        self.use_patch(patch.object(setup_cloudcli, 'cloudcli_websocket'))
        gate = self.use_patch(patch.object(setup_cloudcli, 'verify_cloudcli_agent',
            side_effect=[gateway.GatewayError('fixture failure'), None]))
        login = self.use_patch(patch.object(setup_cloudcli, 'cloudcli_web_login', return_value='fixture.web.token'))
        seen = []

        def auth(path, payload, token=None):
            seen.append(path)
            if path == '/api/auth/register':
                return {'success': True, 'user': {'username':payload['username']}, 'token':'fixture.web.token'}
            self.assertEqual(token, 'fixture.web.token')
            return {'success':True, 'apiKey':{'apiKey':API_KEY}}

        with patch.object(setup_cloudcli, 'cloudcli_auth_request', side_effect=auth):
            with self.assertRaisesRegex(gateway.GatewayError, 'fixture failure'):
                setup_cloudcli.prepare_public_gateway(self.home, self.working_state, {})
            self.assertFalse(self.config_path.exists())
            result = setup_cloudcli.prepare_public_gateway(self.home, self.working_state, {})
        self.assertEqual(seen, ['/api/auth/register','/api/settings/api-keys'])
        self.assertEqual(needs_setup.call_count, 2)
        self.assertEqual(gate.call_count, 2)
        login.assert_called_once_with(3001,result['cloudcliAccount'])
        self.assertGreaterEqual(len(result['cloudcliAccount']['password']), 24)
        self.assertTrue(gateway.load_cloudcli_config()['desktopToolApprovals'])
        self.assertEqual(gateway.load_cloudcli_config()['apiKey'], API_KEY)
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_local_desktop_question_token_is_private_short_lived_and_reloaded(self):
        token_path = self.home/'question.jwt'
        backend = self.manager.cloudcli
        backend.config['desktopQuestions'] = {'tokenFile': str(token_path)}

        def write_token(remaining):
            claims = json.dumps({'exp': int(time.time())+remaining}).encode()
            payload = base64.urlsafe_b64encode(claims).decode().rstrip('=')
            token = 'header.'+payload+'.signature'
            token_path.write_text(token+'\n')
            token_path.chmod(0o600)
            return token

        with patch.object(gateway, 'cloudcli_web_login') as login:
            token = write_token(90)
            self.assertEqual(backend.question_token(), token)
            replacement = write_token(120)
            self.assertEqual(backend.question_token(), replacement)
            for remaining in (-1, 600):
                write_token(remaining)
                with self.assertRaisesRegex(gateway.CloudCliError, 'unavailable or expired'):
                    backend.question_token()
            write_token(90)
            token_path.chmod(0o644)
            with self.assertRaises(gateway.CloudCliError):
                backend.question_token()
            original = self.home/'real.jwt'
            token_path.chmod(0o600)
            token_path.rename(original)
            token_path.symlink_to(original)
            with self.assertRaises(gateway.CloudCliError):
                backend.question_token()
            login.assert_not_called()

        settings = {**self.config, 'desktopQuestions': {'tokenFile': '/run/cloudcli-fixture/session.jwt'},
                    'desktopToolApprovals': True}
        self.assertEqual(gateway.cloudcli_config(settings)['desktopQuestions'], settings['desktopQuestions'])
        with patch.object(public_gateway, 'load_cloudcli_config', return_value=gateway.cloudcli_config(settings)):
            with self.assertRaisesRegex(ValueError, 'Direct CloudCLI'):
                public_gateway.PublicServer(self.home/'direct', {'bind': '0.0.0.0', 'port': 0})

    def test_unconfigured_gateway_refuses_models_and_messages_even_when_an_old_key_is_enabled(self):
        public_state = self.home / 'public-state'
        (public_state / 'keys').mkdir(parents=True)
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-nodes', '-keyout', str(public_state / 'key.pem'), '-out', str(public_state / 'cert.pem'),
            '-days', '1', '-subj', '/CN=SafeCode Test Fixture',
        ], check=True, capture_output=True, timeout=5)

        # A legacy public gateway is upgraded before its operator configures CloudCLI.
        configured = self.config_path.read_text()
        self.config_path.unlink()
        key = 'a' * 43
        digest = hashlib.sha256(key.encode()).hexdigest()
        (public_state/'keys'/digest).touch()
        with patch.object(public_gateway, 'load_cloudcli_config', return_value=None) as startup_config:
            with public_gateway.PublicServer(public_state, {'bind': '127.0.0.1', 'port': 0}) as public:
                self.config_path.write_text(configured)
                self.config_path.chmod(0o600)
                self.assertIsNotNone(gateway.load_cloudcli_config())
                (public_state/public_gateway.PUBLIC_ENABLED).touch()
                worker = threading.Thread(target=public.serve_forever, daemon=True)
                worker.start()
                try:
                    for method, path in (('GET', '/v1/models'), ('GET', '/v1/cloudcli/models'),
                                         ('POST', '/v1/messages'), ('POST', '/v1/cloudcli/messages')):
                        conn = http.client.HTTPSConnection('127.0.0.1', public.server_port, timeout=3,
                                                           context=ssl._create_unverified_context())
                        try:
                            conn.request(method, path, b'{}' if method == 'POST' else None,
                                         {'Authorization': 'Bearer '+key})
                            response = conn.getresponse()
                            self.assertEqual(response.status, 503)
                            response.read()
                        finally:
                            conn.close()
                    self.assertFalse(public.accounts)
                    startup_config.assert_called_once_with()
                finally:
                    public.shutdown()
                    worker.join()
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_public_key_model_permissions_expiry_and_renewal(self):
        self.use_automatic_models(web_catalog=True)
        self.native_catalog.return_value = {'data': []}
        self.cloud.model_options = [{'value': name, 'label': label} for name, label in (
            ('default', 'Default'), ('fable', 'Fable'), ('fable[1m]', 'Fable (1M)'), ('vendor/model', 'Custom Model'))]
        state = self.home/'public-policy'
        (state/'keys').mkdir(parents=True)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=Fixture', '-keyout', state/'key.pem', '-out', state/'cert.pem'],
                       check=True, capture_output=True, timeout=5)
        key, legacy = 'a'*43, 'b'*43
        digest, legacy_digest = (hashlib.sha256(value.encode()).hexdigest() for value in (key, legacy))
        (state/'keys'/legacy_digest).touch()
        public = public_gateway.PublicServer(state, {'bind': '127.0.0.1', 'port': 0, 'publicOrigin': 'https://fixture.test'})
        self.start(public)
        public.manage({'action': 'enable_public', 'accepted': True})

        def manage(action, **args):
            return public.manage({'action': action, **args})

        with patch.object(public_gateway, 'make_key', return_value=('sc2.fixture', digest)), \
                patch.object(public_gateway, 'valid_activation_code', return_value=True):
            with self.assertRaisesRegex(ValueError, '至少选择'):
                manage('issue', allowed_models=[])
            with self.assertRaisesRegex(ValueError, '失效时间'):
                manage('issue', expires_at=True)
            expiry = int(time.time())+3600
            manage('issue', note='policy', expires_at=expiry, allowed_models=['fable', 'fable'])

        def request(path, model=None, token=key, beta=None):
            connection = http.client.HTTPSConnection('127.0.0.1', public.server_port, timeout=4,
                                                     context=ssl._create_unverified_context())
            with closing(connection):
                headers = {'Authorization': 'Bearer '+token}
                if beta:
                    headers['anthropic-beta'] = beta
                connection.request('GET' if model is None else 'POST', path,
                                   json.dumps(conversation('Fixture request', model=model)) if model else None, headers)
                reply = connection.getresponse()
                return reply.status, json.loads(reply.read())

        def listed(key_id=digest):
            return next(row for row in manage('list')['keys'] if row['id'] == key_id)

        self.assertEqual([m['id'] for m in manage('model_catalog')['models']],
                         ['default', 'fable', 'fable[1m]', 'vendor/model'])
        self.assertIsNone(listed(legacy_digest)['allowed_models'])
        self.assertIsNone(listed(legacy_digest)['expires_at'])
        self.assertEqual(listed()['allowed_models'], ['fable'])
        manage('update', id=digest, note='renamed')
        self.assertEqual(listed()['expires_at'], expiry)
        self.assertEqual(len(request('/v1/models', token=legacy)[1]['data']), 3)
        fable, = request('/v1/cloudcli/models')[1]['data']
        self.assertEqual(fable['id'], 'fable')
        self.assertFalse(fable['supports_1m'])
        for model in ('default', 'vendor/model', 'fable[1m]'):
            status, error = request('/v1/cloudcli/messages', model)
            self.assertEqual((status, error['error']['type']), (403, 'permission_error'))
        self.assertFalse(self.cloud.requests)
        self.assertEqual(request('/v1/messages', 'safecode-cloudcli-fable')[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['model'], 'fable')
        manage('update', id=digest, allowed_models=['fable[1m]'])
        variant, = request('/v1/models')[1]['data']
        self.assertTrue(variant['prefer_1m'])
        self.assertEqual(request('/v1/messages', 'fable', beta='context-1m-2025-08-07')[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['model'], 'fable[1m]')

        public.queue.resize(1)
        with ThreadPoolExecutor(1) as pool, public.queue.slot(legacy_digest):
            pending = pool.submit(request, '/v1/messages', 'fable[1m]')
            deadline = time.monotonic()+2
            while public.queue.snapshot()[1].get(digest, 0) == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(public.queue.snapshot()[1][digest], 1)
            manage('update', id=digest, expires_at=int(time.time())-1)
            self.assertEqual(pending.result(timeout=2)[0], 401)
        self.assertTrue(listed()['expired'])
        self.assertTrue(manage('list')['activation_required'])
        for path in ('/v1/models', '/v1/cloudcli/models', '/v1/key/balance'):
            self.assertEqual(request(path)[0], 401)
        self.assertEqual(request('/v1/messages', 'fable[1m]')[0], 401)
        with self.assertRaisesRegex(ValueError, '先撤销'):
            manage('delete', id=digest)
        manage('update', id=digest, expires_at=None, allowed_models=None)
        self.assertFalse(listed()['expired'])
        self.assertEqual(request('/v1/messages', 'vendor/model')[0], 200)

    def test_public_gateway_reviews_only_the_latest_user_prompt_before_dispatch(self):
        state = self.home/'public-review'
        (state/'keys').mkdir(parents=True)
        key = 'a'*43
        (state/'keys'/hashlib.sha256(key.encode()).hexdigest()).touch()
        (state/public_gateway.PUBLIC_ENABLED).touch()
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                        '-nodes', '-days', '1', '-subj', '/CN=Fixture', '-keyout', state/'key.pem',
                        '-out', state/'cert.pem'], check=True, capture_output=True, timeout=5)
        public = public_gateway.PublicServer(state, {'bind': '127.0.0.1', 'port': 0})
        self.start(public)

        def request(payload, path='/v1/messages'):
            with closing(http.client.HTTPSConnection('127.0.0.1', public.server_port, timeout=4,
                                                      context=ssl._create_unverified_context())) as connection:
                connection.request('POST', path, json.dumps(payload), {'Authorization': 'Bearer '+key})
                reply = connection.getresponse()
                return reply.status, json.loads(reply.read())

        normal = '帮我看看我电脑上的视频任务运行得怎么样了'
        self.assertFalse(public.manage({'action': 'list'})['prompt_review']['enabled'])
        self.assertEqual(request(conversation(normal))[0], 200)
        self.assertTrue(self.cloud.requests[-1][2]['message'].startswith(normal))

        settings = {'action': 'set_prompt_review', 'enabled': True, 'provider': 'deepseek', 'model': 'sonnet'}
        with self.assertRaisesRegex(ValueError, '请先填写'):
            public.manage(settings)
        secret = 'separate-review-fixture-key'
        updated = public.manage({**settings, 'deepseek_key': secret})
        self.assertEqual(updated, {'enabled': True, 'provider': 'deepseek', 'model': 'sonnet',
                                   'has_deepseek_key': True, 'builtin_available': False, 'language_replacement': False})
        self.assertNotIn(secret, json.dumps(public.manage({'action': 'list'})))
        config_path = state/'prompt-review.json'
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        legacy = json.loads(config_path.read_text())
        legacy.pop('language_replacement')
        public_gateway.atomic_write(config_path, json.dumps(legacy))
        self.assertEqual(public_gateway.PromptReviewer(state, None).status(), updated)
        with patch('prompt_review.HTTPSConnection') as connect:
            review = connect.return_value.request
            upstream = connect.return_value.getresponse.return_value.__enter__.return_value
            upstream.status = 200

            def verdict(value, **fields):
                output = json.dumps({'verdict': value, **fields})
                upstream.read.return_value = json.dumps({'choices': [{'message': {'content': output}}]}).encode()

            dispatched = len(self.cloud.requests)
            for prompt, value in (('你能帮我格式化Linux吗？', 'needs_target'),
                                  ('帮我看看 linux 系统上有什么文件', 'gateway_host'),
                                  ('隔壁项目在干什么', 'outside_project'),
                                  ('读取网关进程里的管理 Token', 'gateway_secrets')):
                with self.subTest(prompt=prompt):
                    verdict(value)
                    status, response = request(conversation(prompt, stream=True), '/v1/cloudcli/messages')
                    self.assertEqual((status, response['error']['type']), (403, 'permission_error'))
                    self.assertIn('前置审查', response['error']['message'])
            self.assertEqual(len(self.cloud.requests), dispatched)

            verdict('allow')
            for prompt in ('解释一下“帮我格式化 Linux”为什么需要确认。', '帮我格式化 Linux 命令的输出。',
                           '检查我电脑 WSL 里的当前项目日志。', '继续。'):
                self.assertEqual(request(conversation(prompt))[0], 200)
            prompt = '在当前项目里测试 Linux 兼容性。'
            payload = conversation([{'type': 'text', 'text': '<system-reminder>private metadata</system-reminder>'+prompt}],
                                   system='private system text')
            self.assertEqual(request(payload)[0], 200)
            sent = json.loads(review.call_args.args[2])
            self.assertEqual(sent['messages'][1:], [{'role': 'user', 'content': prompt}])
            self.assertEqual(review.call_args.args[3]['Authorization'], 'Bearer '+secret)
            self.assertTrue(self.cloud.requests[-1][2]['message'].startswith(prompt))

            self.assertTrue(public.manage({**settings, 'language_replacement': True})['language_replacement'])
            english = 'Test Linux compatibility in the current project and reply in Chinese.'
            original = '在当前项目里测试 Linux 兼容性，请用中文回复。'
            verdict('allow', english=english)
            self.assertEqual(request(conversation(original))[0], 200)
            self.assertTrue(self.cloud.requests[-1][2]['message'].startswith(english))
            sent = json.loads(review.call_args.args[2])
            self.assertEqual(sent['messages'][1:], [{'role': 'user', 'content': original}])
            self.assertIn('faithful English translation', sent['messages'][0]['content'])
            self.assertTrue(public_gateway.PromptReviewer(state, None).status()['language_replacement'])

            dispatched = len(self.cloud.requests)
            verdict('outside_project')
            self.assertEqual(request(conversation('隔壁项目在干什么'))[0], 403)
            verdict('allow')
            self.assertEqual(request(conversation(original))[0], 503)
            upstream.read.return_value = b'{"choices":[]}'
            self.assertEqual(request(conversation('查看项目状态'))[0], 503)
            self.assertEqual(len(self.cloud.requests), dispatched)
            connect.reset_mock()
            saved = public.manage({**settings, 'enabled': False})
            self.assertTrue(saved['has_deepseek_key'])
            self.assertFalse(saved['language_replacement'])
            self.assertEqual(request(conversation(normal))[0], 200)
            connect.assert_not_called()
        self.assertNoNativeFallback()

    def test_builtin_prompt_review_uses_tool_denied_cloudcli_chat_and_cleans_up(self):
        self.enable_cloudcli_web_fixture()
        backend = self.manager.cloudcli
        reviewer = public_gateway.PromptReviewer(self.working_state, backend.config)
        actions = []

        def run(model, frames, translate=False):
            reviewer.update({'enabled': True, 'provider': 'builtin', 'model': model,
                             'language_replacement': translate}, public_gateway.atomic_write)
            events = ({**event, 'sessionId': CLOUD_SESSION} for event in frames)
            with patch.object(gateway, 'cloudcli_websocket', return_value=(
                    lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
                return reviewer('在当前项目运行测试。')

        run('sonnet', [{'kind': 'text', 'role': 'assistant', 'content': '{"verdict":"allow"}'},
                       {'kind': 'complete', 'exitCode': 0}])
        message, = actions
        self.assertEqual(message['options']['model'], 'sonnet')
        self.assertEqual(message['options']['permissionMode'], 'dontAsk')
        self.assertEqual(message['options']['toolsSettings'], {'allowedTools': [], 'disallowedTools': ['*'], 'skipPermissions': False})
        self.assertEqual(message['options']['attachments'], [])
        project = Path(self.cloud.created_sessions[0]['projectPath'])
        self.assertEqual(json.loads((project/'.claude/settings.json').read_text())['permissions'], {'deny': ['*']})

        english = 'Run tests in the current project.'
        self.assertEqual(run('opus', [
            {'kind': 'text', 'role': 'assistant', 'content': json.dumps({'verdict': 'allow', 'english': english})},
            {'kind': 'complete', 'exitCode': 0},
        ], translate=True), english)
        self.assertEqual(actions[-1]['options']['model'], 'opus')
        self.assertIn('faithful English translation', actions[-1]['content'])
        with self.assertRaisesRegex(gateway.CloudCliError, '前置审查暂时不可用'):
            run('opus', [{'kind': 'tool_use', 'toolName': 'FixtureTool'}])
        self.assertEqual(actions[-2]['options']['model'], 'opus')
        self.assertEqual(actions[-1], {'type': 'chat.abort', 'sessionId': CLOUD_SESSION})
        self.assertEqual(self.cloud.deleted_sessions, [f'/api/providers/sessions/{CLOUD_SESSION}?force=true&deletedFromDisk=true'] * 3)
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_desktop_title_request_does_not_start_cloud_agent(self):
        title = ('You are coming up with a succinct title and git branch name for a coding session '
                 'based on this conversation. Here is the session description:\n'
                 '<description>Potentially side-effectful actual task.</description>')
        self.session = ''
        payload = conversation(title, max_tokens=64)
        self.assertEqual(self.request(payload)[0], 400)
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_desktop_gateway_probe_and_title_helpers_are_local(self):
        self.session = ''
        for prompt in ('.', 'Hi', 'Hello'):
            status, response = self.request(conversation(prompt, max_tokens=1))
            self.assertEqual(status, 200)
            self.assertEqual(response['content'][0]['text'], 'OK')
            self.assertEqual(sum(response['usage'].values()), 0)
        self.assertEqual(self.request(conversation('.'))[0], 400)
        title = ('You are coming up with a succinct title for an agent chat session based on the provided description.\n'
                 'Here is the session description:\n<description>整理示例项目的文件</description>\n'
                 'Please generate a title for this session, in the predominant language of the description.')
        branch = ('You are coming up with a succinct title and git branch name for a coding session based on the provided description.\n'
                  'Here is the session description:\n<description>Review the project structure</description>\n'
                  'Please generate a title and branch name for this session.')
        for prompt, expected in ((title, '<title>整理示例项目的文件</title>'),
                                 (branch, '<title>Review the project structure</title>')):
            status, response = self.request(conversation(prompt, max_tokens=200))
            self.assertEqual(status, 200)
            self.assertTrue(response['content'][0]['text'].startswith(expected))
            if prompt == branch:
                self.assertRegex(response['content'][0]['text'], r'<branch>claude/[a-z0-9-]+</branch>$')
            self.assertEqual(sum(response['usage'].values()), 0)
        self.assertFalse(self.cloud.requests)
        with closing(sqlite3.connect(self.manager.cloudcli.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.assertNoNativeFallback()

    def test_scoped_punctuation_follow_up_is_a_user_turn(self):
        status, reply = self.request(conversation('Show a fixture.'))
        self.assertEqual(status, 200)
        self.assertEqual(self.request(conversation('unused', messages=[
            {'role': 'user', 'content': 'Show a fixture.'}, {'role': 'assistant', 'content': reply['content']},
            {'role': 'user', 'content': '.'},
        ]))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['message'], '.')
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

    def test_questions_and_approvals_reference_the_original_session(self):
        self.config['webOrigin'] = 'https://cloud.example.com/'
        self.save_config()
        self.start_gateway()
        questions = {'questions': [
            {'header': '颜色', 'question': '选择适合示例的颜色', 'multiSelect': True, 'options': [
                {'label': '蓝色', 'description': '海蓝色'},
                {'label': '[外部链接](https://example.org)', 'description': '作为文字显示'},
            ]},
            {'header': '格式', 'question': '选择输出格式', 'multiSelect': False,
             'options': [{'label': '纯文本', 'description': '不含样式'}]},
        ]}
        self.cloud.reply = (200, [
            {'type': 'session-id', 'sessionId': CLOUD_SESSION},
            {'kind': 'session_created', 'newSessionId': 'provider-fixture-id'},
            {'kind': 'tool_use', 'toolName': 'AskUserQuestion', 'toolId': 'tool-question', 'toolInput': questions},
            {'kind': 'permission_request', 'requestId': 'request-question', 'toolName': 'AskUserQuestion',
             'input': questions},
            {'kind': 'permission_resolved', 'requestId': 'request-question'},
            {'kind': 'permission_request', 'requestId': 'request-plan', 'toolName': 'ExitPlanMode',
             'input': {'plan': 'Review the example plan'}},
            {'kind': 'permission_cancelled', 'requestId': 'request-plan', 'reason': 'fixture cancelled'},
            {'kind': 'complete', 'exitCode': 0}, {'type': 'done'},
        ])
        self.cloud.pause_after_frame = 3
        self.cloud.release_stream = threading.Event()
        payload = conversation('Ask a question in the fixture', stream=True, tools=[
            {'name': 'AskUserQuestion', 'input_schema': {'type': 'object'}},
        ])
        headers = {'Authorization': f'Bearer {DESKTOP_TOKEN}', 'x-claude-code-session-id': self.session}
        with closing(http.client.HTTPConnection(*self.desktop.server_address, timeout=3)) as conn:
            try:
                conn.request('POST', '/v1/messages', json.dumps(payload), headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                events = []
                for row in response:
                    if row.startswith(b'data: '):
                        events.append(json.loads(row[6:]))
                        if '正在等待用户输入' in events[-1].get('delta', {}).get('text', ''):
                            break
                self.assertIn('正在等待用户输入', events[-1]['delta']['text'])
                self.assertNotIn('message_stop', [event['type'] for event in events])
            finally:
                self.cloud.release_stream.set()
            events.extend(json.loads(row[6:]) for row in response if row.startswith(b'data: '))
        visible = ''.join(event['delta']['text'] for event in events if event['type'] == 'content_block_delta')
        self.assertEqual(visible.count('选择适合示例的颜色'), 1)
        for expected in ('颜色（多选）', '选择输出格式', '格式（单选）', '海蓝色', '其他',
                         '>     2. [外部链接]', '正在等待用户输入', '已收到处理结果',
                         'Review the example plan', '等待已取消', 'fixture cancelled',
                         f'https://cloud.example.com/session/{CLOUD_SESSION}', '未返回额外的助手文字'):
            self.assertIn(expected, visible)
        self.assertEqual(events[-1]['type'], 'message_stop')
        self.assertNotIn('provider-fixture-id', visible)
        self.assertFalse(any(event.get('content_block', {}).get('type') == 'tool_use' for event in events))

    def test_native_desktop_question_returns_answer_to_the_same_cloudcli_run(self):
        credentials = {'username': 'fixture-user', 'password': 'fixture-password'}
        self.config['desktopQuestions'] = credentials
        self.save_config()
        self.start_gateway()
        question = 'Pick a "fixture" color.'
        second = 'Choose a sample style.'
        questions = {'questions': [
            {'header': 'Colors', 'question': question, 'multiSelect': True, 'options': [
                {'label': 'Blue', 'description': 'Ocean blue'}, {'label': 'Green', 'description': 'Leaf green'},
            ]},
            {'header': 'Styles', 'question': second, 'multiSelect': False, 'options': [
                {'label': 'Casual', 'description': 'Casual look'}, {'label': 'Formal', 'description': 'Formal look'},
            ]},
        ]}
        pending = {'requestId': 'fixture-approval', 'toolName': 'AskUserQuestion', 'input': questions,
                   'sessionId': CLOUD_SESSION}
        actions = []
        accepted = threading.Event()
        heartbeat = threading.Event()

        def on_send(command):
            actions.append(command)
            if command['type'] == 'chat.permission-response':
                accepted.set()

        def cloud_events():
            # The Agent API and bypassPermissions WebSocket runs do not ask.
            if actions[-1]['options']['permissionMode'] != 'default':
                yield {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}
                return
            yield {'kind': 'tool_use', 'sessionId': CLOUD_SESSION, 'toolName': 'AskUserQuestion',
                   'toolId': 'fixture-tool', 'toolInput': questions}
            yield {'kind': 'permission_request', **pending}
            self.assertTrue(accepted.is_set())
            yield {'kind': 'permission_resolved', 'sessionId': CLOUD_SESSION, 'requestId': pending['requestId']}
            yield {'kind': 'text', 'sessionId': CLOUD_SESSION, 'role': 'assistant',
                   'content': 'Fixture selections accepted.'}
            yield {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}

        class HeartbeatSocket(ChatSocketFixture):
            def pong(self):
                heartbeat.set()

        def connect(*args, timeout, **kwargs):
            if timeout != 90:
                return ChatSocketFixture(on_send, [{'kind': 'chat_subscribed', 'sessionId': CLOUD_SESSION,
                    'isProcessing': True, 'pendingPermissions': [pending]}])
            if not actions:
                return HeartbeatSocket(on_send, cloud_events())
            return ChatSocketFixture(on_send, [
                {'kind': 'text', 'sessionId': CLOUD_SESSION, 'role': 'assistant', 'content': 'Fixture follow-up.'},
                {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0},
            ])

        with patch.object(gateway, 'cloudcli_websocket', return_value=(connect, OSError)), \
                patch.object(gateway.CloudCliSocketEvents, 'PAUSED_HEARTBEAT_SECONDS', .01):
            tools = [{'name': 'AskUserQuestion', 'input_schema': {'type': 'object'}}]
            first = conversation('Choose colors and style for the fixture.', stream=True, tools=tools)
            status, events = self.request(first)
            self.assertEqual(status, 200)
            self.assertEqual(events[-2]['delta']['stop_reason'], 'tool_use')
            parsed = gateway.StreamMessage()
            for event in events:
                parsed.feed(event)
            content = parsed.partial()
            card = content[-1]
            self.assertEqual((card['type'], card['name'], card['input']), ('tool_use', 'AskUserQuestion', questions))
            self.assertNotIn('打开 CloudCLI', json.dumps(content, ensure_ascii=False))
            self.assertFalse(accepted.is_set())
            self.assertTrue(heartbeat.wait(1), 'The upstream socket must stay alive while Desktop waits')
            self.assertEqual(self.request(first), (200, events))
            self.assertEqual(self.cloud.logins, [credentials])
            dispatch, = actions
            self.assertEqual(dispatch['type'], 'chat.send')
            self.assertEqual(dispatch['sessionId'], CLOUD_SESSION)
            rendered = (f'Your questions have been answered: "{question}"="Blue, Green", '
                        f'"{second}"="Custom, with quotes \"yes\"". You can now continue with these answers in mind.'
                        '\n\n<system-reminder>filtered by the gateway</system-reminder>')
            result = {'type': 'tool_result', 'tool_use_id': card['id'], 'content': rendered}
            follow_messages = [*first['messages'], {'role': 'assistant', 'content': content},
                               {'role': 'user', 'content': [result]}]
            second_request = conversation('unused', messages=follow_messages, tools=tools)
            self.assertEqual(self.request(second_request, session=str(uuid.uuid4()))[0], 409)
            self.assertEqual(self.request(conversation('Do something different.'))[0], 409)
            self.assertEqual(actions, [dispatch])
            status, reply = self.request(second_request)
            self.assertEqual(status, 200)
            self.assertIn('Fixture selections accepted.', reply['content'][0]['text'])
            self.assertEqual(self.request(second_request), (200, reply))
            self.assertEqual(actions, [
                dispatch,
                {'type': 'chat.subscribe', 'sessions': [{'sessionId': CLOUD_SESSION}]},
                {'type': 'chat.permission-response', 'allow': True, 'requestId': pending['requestId'],
                 'updatedInput': {**questions, 'answers': {question: 'Blue, Green', second: 'Custom, with quotes "yes"'}}},
            ])
            follow_up = conversation('unused', messages=[*follow_messages, {'role': 'assistant', 'content': reply['content']},
                                                        {'role': 'user', 'content': 'Continue in the same chat.'}], tools=tools)
            status, reply = self.request(follow_up)
            self.assertEqual(status, 200)
            self.assertIn('Fixture follow-up.', reply['content'][0]['text'])
            self.assertEqual((actions[-1]['type'], actions[-1]['sessionId']), ('chat.send', CLOUD_SESSION))
            self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_status_and_compact_activity_without_streaming(self):
        first = 'Inspect sample data.'
        self.cloud.reply = (200, [
            {'type': 'session-id', 'sessionId': CLOUD_SESSION},
            {'kind': 'history_truncated', 'summary': 'Earlier context was trimmed.'},
            {'kind': 'status', 'text': 'Provider setup complete'},
            {'kind': 'text', 'role': 'user', 'isLocalCommand': True, 'content': '/compact'},
            {'kind': 'text', 'role': 'assistant', 'compact': {'phase': 'running'}, 'content': 'Compaction started'},
            {'kind': 'text', 'role': 'assistant', 'isLocalCommandStdout': True, 'content': 'Command output'},
            {'kind': 'tool_use', 'toolName': 'Agent', 'toolId': 'parent'},
            {'kind': 'thinking', 'parentToolUseId': 'parent', 'content': 'Sample subagent thought'},
            {'kind': 'tool_use', 'toolName': 'Read', 'toolId': 'reader', 'parentToolUseId': 'parent',
             'toolInput': {'file_path': 'sample.txt'}},
            {'kind': 'tool_result', 'toolId': 'reader', 'parentToolUseId': 'parent', 'content': json.dumps([
                {'type': 'text', 'text': 'Sample file output'},
                {'type': 'image', 'source': {'type': 'base64', 'data': 'opaque-fixture-image'}},
            ]), 'isError': True},
            {'kind': 'stream_delta', 'parentToolUseId': 'parent', 'content': 'Sample agent answer'},
            {'kind': 'text', 'role': 'assistant', 'parentToolUseId': 'parent', 'content': 'Sample agent answer'},
            {'kind': 'stream_delta', 'parentToolUseId': 'parent', 'content': 'Another partial child reply'},
            {'kind': 'task_status', 'taskId': 'workflow-fixture', 'event': 'progress',
             'agents': [{'label': 'fixture agent', 'state': 'running', 'lastToolName': 'Read'}]},
            {'kind': 'task_notification', 'taskId': 'workflow-fixture', 'status': 'completed', 'summary': 'Sample workflow finished'},
            {'kind': 'status', 'text': 'token_budget', 'tokenBudget': {'used': 125, 'total': 1000, 'inputTokens': 100}},
            {'kind': 'different_new_event', 'content': 'Fixture opaque payload'},
            {'kind': 'stream_delta', 'content': 'Fixture reply'}, {'kind': 'stream_end'},
            {'kind': 'text', 'role': 'assistant', 'content': 'Fixture reply'},
            {'kind': 'complete', 'exitCode': 0}, {'type': 'done'},
        ])
        status, reply = self.request(conversation(first))
        self.assertEqual(status, 200)
        self.assertEqual(reply['usage']['input_tokens'], 100)
        visible = reply['content'][0]['text']
        self.assertEqual(visible.count('Fixture reply'), 1)
        self.assertEqual(visible.count('CloudCLI 正在执行云端操作'), 1)
        for expected in ('Earlier context was trimmed.', 'Provider setup complete', '/compact', 'Compaction started',
                         'Command output', '工具调用 2 次', '后台任务 1 项', '报告失败 1 项', '125 / 1,000',
                         'different_new_event'):
            self.assertIn(expected, visible)
        for detail in ('Sample subagent thought', 'sample.txt', 'Sample file output', 'Sample agent answer',
                       'Another partial child reply', 'fixture agent', 'Sample workflow finished',
                       'Fixture opaque payload', 'opaque-fixture-image'):
            self.assertNotIn(detail, visible)
        self.cloud.reply = (200, cloud_turn('Follow-up reply'))
        self.assertEqual(self.request(conversation('unused', messages=[
            {'role': 'user', 'content': first}, {'role': 'assistant', 'content': reply['content']},
            {'role': 'user', 'content': 'Follow up.'},
        ]))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

    def enable_cloudcli_web_fixture(self):
        self.config['desktopQuestions'] = {'username': 'fixture-user', 'password': 'fixture-password'}
        self.save_config()
        self.start_gateway()

    def test_attachments_use_cloudcli_upload_and_the_same_conversation(self):
        self.enable_cloudcli_web_fixture()
        self.manager.cloudcli.device = {'id': CLIENT_ID, 'platform': 'macos', 'hostname': 'Example Mac',
                                       'alias': 'desk-mac', 'command': 'ssh desk-mac'}
        actions = []
        events = [{'kind': 'text', 'role': 'assistant', 'content': 'Fixture attachments received.', 'sessionId': CLOUD_SESSION},
                  {'kind': 'complete', 'exitCode': 0, 'sessionId': CLOUD_SESSION}]
        screenshot = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l5sAAAAASUVORK5CYII=')
        pdf = b'%PDF-1.4\nFixture document'
        content = [{'type': 'text', 'text': 'Inspect these fixtures.'},
                   {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                              'data': base64.b64encode(screenshot).decode()}},
                   {'type': 'document', 'title': 'Report.pdf', 'source': {'type': 'base64', 'media_type': 'application/pdf',
                                                                        'data': base64.b64encode(pdf).decode()}}]
        payload = conversation('unused', messages=[{'role': 'user', 'content': content}],
                               system=desktop_environment('/Users/example/Work'))
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
            status, reply = self.request(payload)
            self.assertEqual(status, 200)
            self.assertIn('Fixture attachments received.', reply['content'][0]['text'])
            self.assertEqual(self.request(payload), (200, reply))
            self.assertEqual(len(self.cloud.created_sessions), 1)
            session = self.cloud.created_sessions[0]
            self.assertEqual(session['initialMessage'], 'Inspect these fixtures.')
            self.assertEqual(self.cloud.registered_projects, [session['projectPath']])
            self.assertEqual(self.cloud.uploads, [('attachment-1.png', 'image/png', screenshot),
                                                  ('Report.pdf', 'application/pdf', pdf)])
            self.assertEqual(len(actions), 1)
            command = actions[0]
            self.assertEqual((command['type'], command['sessionId']), ('chat.send', CLOUD_SESSION))
            self.assertTrue(command['content'].startswith('Inspect these fixtures.\n\n本机是'))
            self.assertIn('/Users/example/Work', command['content'])
            self.assertEqual([a['path'] for a in command['options']['attachments']],
                             ['/example/assets/0-attachment-1.png', '/example/assets/1-Report.pdf'])
            self.assertFalse(self.cloud.requests)
            follow_up = [*payload['messages'], {'role': 'assistant', 'content': reply['content']},
                         {'role': 'user', 'content': 'Continue in the same conversation.'}]
            self.assertEqual(self.request(conversation('unused', messages=follow_up))[0], 200)
            self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
            self.assertEqual(self.cloud.requests[-1][2]['message'], 'Continue in the same conversation.')
            content[-1]['source'] = {'type': 'url', 'url': 'https://example.com/report.pdf'}
            self.assertEqual(self.request(payload, session=str(uuid.uuid4()))[0], 400)
            self.assertEqual(len(actions), 1)

    def test_large_combined_attachments_and_attachment_only_turn(self):
        self.enable_cloudcli_web_fixture()
        actions = []
        events = [{'kind': 'text', 'role': 'assistant', 'content': 'Files received.', 'sessionId': CLOUD_SESSION},
                  {'kind': 'complete', 'exitCode': 0, 'sessionId': CLOUD_SESSION}]
        inline = {'type': 'document', 'source': {'type': 'text', 'data': 'Inline text without a MIME field.'}}
        raw = b'a' * (4 * 1024 * 1024)
        source = {'type': 'base64', 'media_type': 'text/plain', 'data': base64.b64encode(raw).decode()}
        content = [inline, {'type': 'document', 'title': 'one.txt', 'source': source},
                   {'type': 'document', 'title': 'two.txt', 'source': source}]
        payload = conversation('unused', messages=[{'role': 'user', 'content': content}])
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
            self.assertEqual(self.request(payload)[0], 200)
        command, = actions
        self.assertTrue(command['content'].startswith('请查看本轮附加的文件。'))
        self.assertEqual([len(data) for _, _, data in self.cloud.uploads], [len(inline['source']['data']), len(raw), len(raw)])

    def test_desktop_native_text_attachment_preview_does_not_block_first_turn(self):
        self.enable_cloudcli_web_fixture()
        self.manager.cloudcli.device = {'id': CLIENT_ID, 'platform': 'macos', 'hostname': 'Example Mac',
                                       'alias': 'desk-mac', 'command': 'ssh desk-mac'}
        actions = []
        path = '/Users/example/fixture.txt'
        reminder = {'role': 'user', 'content': '<system-reminder>\nCalled the Read tool with the following input: '
                    + json.dumps({'file_path': path}) + '\n</system-reminder>'}
        task = {'role': 'user', 'content': [
            {'type': 'text', 'text': '<system-reminder>\nResult of calling the Read tool:\n1\tSC214-UI-fixture\n2\t\n</system-reminder>'},
            {'type': 'text', 'text': '<system-reminder>\nExtra context is not a preceding turn.\n</system-reminder>'},
            {'type': 'text', 'text': f'@"{path}"\nRead the attachment.'},
        ]}
        events = [{'kind': 'text', 'role': 'assistant', 'content': 'Fixture received.', 'sessionId': CLOUD_SESSION},
                  {'kind': 'complete', 'exitCode': 0, 'sessionId': CLOUD_SESSION}]
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
            status, reply = self.request(conversation('unused', messages=[reminder, task],
                system=desktop_environment('/Users/example/Work')))
        self.assertEqual(status, 200)
        self.assertEqual(self.cloud.uploads, [('fixture.txt', 'text/plain', b'SC214-UI-fixture\n')])
        self.assertNotIn('Result of calling the Read tool', actions[0]['content'])
        self.assertEqual(len(actions[0]['options']['attachments']), 1)
        followup = [reminder, task, {'role': 'assistant', 'content': reply['content']},
                    {'role': 'user', 'content': 'Continue the same conversation.'}]
        self.assertEqual(self.request(conversation('unused', messages=followup))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        self.assertEqual(len(self.cloud.uploads), 1)

    def test_paired_mac_recovers_workspace_from_the_matching_desktop_session(self):
        self.manager.cloudcli.device = {'id': CLIENT_ID, 'platform': 'macos', 'hostname': 'Example Mac',
                                       'alias': 'desk-mac', 'command': 'ssh -F /trusted/fixture desk-mac'}
        session = str(uuid.uuid4())
        with patch('host_control_mcp.exchange_ssh', return_value={'workspace': '/Users/example/Desktop'}) as resolve:
            self.assertEqual(self.request(conversation('Check the selected folder.'), session=session)[0], 200)
        self.assertIn('当前工作目录为 `/Users/example/Desktop`', self.cloud.requests[-1][2]['message'])
        argv, data = resolve.call_args.args
        self.assertEqual(json.loads(data), {'name': 'desktop_workspace', 'session': session})
        self.assertIn('desk-mac', argv)

    def test_paired_mac_reads_environment_system_message_on_the_first_turn(self):
        self.manager.cloudcli.device = {'id': CLIENT_ID, 'platform': 'macos', 'hostname': 'Example Mac',
                                       'alias': 'desk-mac', 'command': 'ssh desk-mac'}
        request = conversation('unused', system='Static instructions.', messages=[
            {'role': 'user', 'content': 'Check the selected folder.'},
            {'role': 'system', 'content': desktop_environment('/Users/example/Desktop')},
        ])
        with patch('host_control_mcp.exchange_ssh') as lookup:
            self.assertEqual(self.request(request)[0], 200)
        lookup.assert_not_called()
        self.assertIn('当前工作目录为 `/Users/example/Desktop`', self.cloud.requests[-1][2]['message'])

    def test_plan_confirmation_displays_full_plan_and_requires_explicit_approval(self):
        self.enable_cloudcli_web_fixture()
        plan = 'Fixture plan body\n'+('Review before making local changes.\n'*180)+'Last step visible.'
        fields = {'allowedPrompts': []}
        pending = {'requestId': 'fixture-plan', 'toolName': 'ExitPlanMode', 'input': fields, 'sessionId': CLOUD_SESSION}
        actions = []
        accepted = threading.Event()

        def on_send(action):
            actions.append(action)
            if action['type'] == 'chat.permission-response':
                accepted.set()

        def cloud_events():
            yield {'kind': 'tool_use', 'toolName': 'ExitPlanMode', 'toolId': 'fixture-plan-tool',
                   'toolInput': {**fields, 'plan': plan}, 'sessionId': CLOUD_SESSION}
            yield {'kind': 'permission_request', **pending}
            if not accepted.wait(2):
                return
            yield {'kind': 'permission_resolved', 'requestId': pending['requestId'], 'sessionId': CLOUD_SESSION}
            yield {'kind': 'text', 'role': 'assistant', 'content': 'Plan confirmed.', 'sessionId': CLOUD_SESSION}
            yield {'kind': 'complete', 'exitCode': 0, 'sessionId': CLOUD_SESSION}

        def open_socket(*args, timeout, **kwargs):
            if timeout == 90:
                return ChatSocketFixture(on_send, cloud_events())
            return ChatSocketFixture(on_send, [{'kind': 'chat_subscribed', 'sessionId': CLOUD_SESSION,
                                                'isProcessing': True, 'pendingPermissions': [pending]}])

        with patch.object(gateway, 'cloudcli_websocket', return_value=(open_socket, OSError)):
            prompt = ('<system-reminder>Plan mode is active. The user indicated that they do not want you to execute yet'
                      '</system-reminder>Prepare a plan.')
            tools = [{'name': 'ExitPlanMode', 'input_schema': {'type': 'object'}}]
            payload = conversation(prompt, tools=tools)
            status, reply = self.request(payload)
            self.assertEqual(status, 200)
            self.assertEqual(reply['stop_reason'], 'tool_use')
            self.assertIn('Last step visible.', reply['content'][0]['text'])
            card = reply['content'][-1]
            self.assertEqual((card['name'], card['input']), ('ExitPlanMode', {'allowedPrompts': []}))
            self.assertEqual(actions[0]['options']['permissionMode'], 'plan')
            self.assertTrue(actions[0]['content'].startswith('Prepare a plan.'))
            self.assertFalse(accepted.is_set())
            result = {'type': 'tool_result', 'tool_use_id': card['id'], 'content': 'yes'}
            follow = [*payload['messages'], {'role': 'assistant', 'content': reply['content']},
                      {'role': 'user', 'content': [result]}]
            self.assertEqual(self.request(conversation('unused', messages=follow))[0], 400)
            self.assertFalse(accepted.is_set())
            result['content'] = 'User has approved your plan. You can now start coding.\n\n## Approved Plan:\nA different plan.'
            self.assertEqual(self.request(conversation('unused', messages=follow))[0], 409)
            self.assertFalse(accepted.is_set())
            result['content'] = ('User has approved exiting plan mode. You can now proceed.\n'
                                 '<system-reminder>Exited Plan Mode</system-reminder>')
            status, answer = self.request(conversation('unused', messages=follow))
            self.assertEqual(status, 200)
            self.assertIn('Plan confirmed.', answer['content'][0]['text'])
            self.assertEqual(actions[-1], {'type': 'chat.permission-response', 'requestId': pending['requestId'],
                                           'allow': True, 'updatedInput': fields})
            self.assertFalse(self.cloud.requests)

    def test_disconnect_stops_only_the_owned_run_and_preserves_continuation(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_running = True
        self.cloud.pause_after_frame = 0
        self.cloud.release_stream = threading.Event()
        self.addCleanup(self.cloud.release_stream.set)
        self.cloud.reply = (200, [{'type': 'session-id', 'sessionId': CLOUD_SESSION},
                                  {'kind': 'complete', 'exitCode': 0, 'aborted': True}])
        stop_requests = []

        def stop(action):
            stop_requests.append(action)
            self.cloud.release_stream.set()

        headers = {'Authorization': f'Bearer {DESKTOP_TOKEN}', 'x-claude-code-session-id': self.session}
        original = conversation('Wait for an explicit stop.', stream=True)
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(stop), OSError)):
            with closing(http.client.HTTPConnection(*self.desktop.server_address, timeout=3)) as conn:
                conn.request('POST', '/v1/messages', json.dumps(original), headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.close()
            self.assertTrue(self.cloud.release_stream.wait(3))
            deadline = time.monotonic()+2
            with closing(sqlite3.connect(self.manager.cloudcli.path)) as db:
                while not db.execute('SELECT 1 FROM sessions WHERE session=?', (CLOUD_SESSION,)).fetchone():
                    if time.monotonic() >= deadline:
                        self.fail('Stopped conversation checkpoint was not retained')
                    time.sleep(.02)
            self.assertEqual(stop_requests, [{'type': 'chat.abort', 'sessionId': CLOUD_SESSION}])
        self.assertEqual(len(self.cloud.requests), 1)
        self.cloud.pause_after_frame = None
        self.cloud.reply = (200, cloud_turn('Continuation after stopping.'))
        following = [{'role':'user','content':[
            {'type':'text','text':original['messages'][0]['content']+'\n'},
            {'type':'text','text':'[Request interrupted by user]\n'},
            {'type':'text','text':'An earlier rejected local attempt.\n'},
            {'type':'text','text':'[Request interrupted by user]\n'},
            {'type':'text','text':'Continue after stopping.'},
        ]}]
        self.assertEqual(self.request(conversation('unused', messages=following))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        task = self.cloud.requests[-1][2]['message']
        self.assertTrue(task.startswith('Continue after stopping.'))
        self.assertNotIn('An earlier rejected local attempt',task)

    def test_stopped_prompt_can_be_edited_before_cloudcli_recorded_a_user_anchor(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        first = conversation('Retain this context.')
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        prefix = [*first['messages'],{'role':'assistant','content':reply['content']}]
        backend = self.manager.cloudcli
        scope = backend.identity+':'+self.session
        history = gateway.history_signature(prefix, 'cloudcli')
        anchor = self.cloud.history[-1]['transcriptAnchorId']
        with closing(sqlite3.connect(backend.path)) as db, db:
            db.execute('INSERT INTO stopped_before_anchor VALUES (?,?,?,?)',(scope,CLOUD_SESSION,history,anchor))
            db.execute('UPDATE session_state SET tip_anchor=NULL WHERE scope=?',(scope,))
            db.execute('DELETE FROM sessions WHERE scope=?',(scope,))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(scope,gateway.history_signature(
                prefix+[{'role':'user','content':'Original canceled prompt.'}],'cloudcli'),CLOUD_SESSION,time.time()))
        task = conversation('unused', messages=[*prefix,{'role':'user','content':'Edited canceled prompt.'}])
        self.cloud.history.append(cloud_user('An unexpected native request.'))
        self.assertEqual(self.request(task)[0],409)
        self.cloud.history.pop()
        self.assertEqual(self.request(task)[0],200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'],CLOUD_SESSION)
        self.assertTrue(self.cloud.requests[-1][2]['message'].startswith('Edited canceled prompt.'))

    def test_new_desktop_scope_forks_only_an_exact_verified_prefix(self):
        self.enable_cloudcli_web_fixture()
        self.cloud.track_history = True
        prompt = conversation('Original conversation context.')
        status, reply = self.request(prompt)
        self.assertEqual(status, 200)
        original = self.cloud.history[-1].copy()
        fork_id = 'native-cloudcli-fork-fixture'
        prefix = [*prompt['messages'], {'role':'assistant','content':reply['content']}]
        branch = conversation('unused',messages=[*prefix,{'role':'user','content':'Continue this branch.'}])
        corrupted = conversation('unused',messages=[*prefix[:-1],{'role':'assistant','content':'Different history.'},
                                                     branch['messages'][-1]])
        backend = self.manager.cloudcli
        requests = []
        native_request = backend.web_request

        def web_request(method,path,*args,**kwargs):
            if path.endswith('/fork'):
                requests.append((method,path,json.loads(args[0])))
                return {'data':{'sessionId':fork_id}}
            return native_request(method,path,*args,**kwargs)

        self.cloud.track_history = False
        self.cloud.reply = (200,cloud_turn('Continued the fork.',session_id=fork_id))
        with patch.object(backend,'history_users',side_effect=lambda *a, **kw:[original]), \
                patch.object(backend,'web_request',side_effect=web_request):
            self.assertEqual(self.request(corrupted,session=str(uuid.uuid4()))[0],409)
            self.assertEqual(self.request(branch,session=str(uuid.uuid4()))[0],200)
        self.assertEqual(requests,[('POST',f'/api/providers/sessions/{CLOUD_SESSION}/fork',{})])
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'],fork_id)
        self.assertEqual(self.cloud.history[-1],original)

    def test_native_bounded_fork_can_recreate_its_last_user_anchor(self):
        backend = self.manager.cloudcli
        original = [cloud_user('Verified preceding prompt.'),cloud_user('Prompt to replace.')]
        fork_id = 'verified-fork-session'
        cloned = [original[0].copy(),{**cloud_user(original[-1]['content']), 'timestamp':'2026-11-23T10:00:01Z'}]
        original[-1]['timestamp'] = '2026-11-23T10:00:00Z'
        tip = original[-1]['transcriptAnchorId']
        branch = ('source-scope',CLOUD_SESSION,None,None,MODEL,tip,tip)
        self.assertNotEqual(cloned[-1]['transcriptAnchorId'],tip)
        with patch.object(backend,'running_sessions',return_value=[]), \
                patch.object(backend,'history_users',side_effect=lambda session, **kwargs:original if session==CLOUD_SESSION else cloned), \
                patch.object(backend,'web_request',return_value={'data':{'sessionId':fork_id}}) as native:
            route,edit = backend.fork_desktop_branch(branch)
            self.assertEqual(route[0],fork_id)
            self.assertEqual(edit,(cloned[-1]['transcriptAnchorId'],)*2)
            self.assertEqual(json.loads(native.call_args.args[2]),{'upToAnchorId':tip})
            cloned[0]['content'] = 'An unexpected previous prompt.'
            with self.assertRaisesRegex(gateway.CloudCliError,'verified Desktop prefix'):
                backend.fork_desktop_branch(branch)

    def test_stop_does_not_cancel_a_replacement_run(self):
        actions = []
        progress = gateway.CloudCliProgress(object(), None)
        stop = gateway.CloudCliStopWatch(self.manager.cloudcli, progress, 100)
        stop.session, stop.opened_ms, stop.started_at = CLOUD_SESSION, 200, 140
        with patch.object(gateway, 'cloudcli_websocket', return_value=(lambda: None, OSError)), \
                patch.object(self.manager.cloudcli, 'web_socket', return_value=ChatSocketFixture(actions.append)), \
                patch.object(self.manager.cloudcli, 'running_since', return_value=220):
            stop.stop(progress.stream)
        self.assertFalse(actions)
        self.assertFalse(progress.stopping.is_set())

    def test_switching_model_uses_the_existing_cloudcli_session(self):
        opus = 'claude-opus-5-5'
        self.config['models'].append(opus)
        self.enable_cloudcli_web_fixture()
        first = conversation('Start on the configured default.')
        status, original = self.request(first)
        self.assertEqual(status, 200)
        updated = [*first['messages'], {'role': 'assistant', 'content': original['content']},
                   {'role': 'user', 'content': 'Continue on the new model.'}]
        actions = []
        events = [{'kind': 'text', 'role': 'assistant', 'content': 'The selected model replied.',
                   'model': opus, 'sessionId': CLOUD_SESSION},
                  {'kind': 'complete', 'exitCode': 0, 'sessionId': CLOUD_SESSION}]
        payload = conversation('unused', model=opus, messages=updated)
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
            status, switched = self.request(payload)
            self.assertEqual(status, 200)
            self.assertEqual(self.request(payload), (200, switched))
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertFalse(self.cloud.created_sessions)
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0]['type'], actions[0]['sessionId'], actions[0]['options']['model']),
                         ('chat.send', CLOUD_SESSION, opus))

    def test_edit_only_rewinds_to_a_verified_anchor_in_the_same_conversation(self):
        self.enable_cloudcli_web_fixture()
        self.manager.cloudcli.device = {'id': CLIENT_ID, 'platform': 'macos', 'hostname': 'Example Mac',
                                       'alias': 'desk-mac', 'command': 'ssh desk-mac'}
        self.cloud.track_history = True
        first = conversation('The original first question', system=desktop_environment('/Users/example/Work'))
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        prefix = [*first['messages'], {'role': 'assistant', 'content': reply['content']}]
        self.cloud.history.append({'role': 'assistant', 'kind': 'text', 'content': 'x' * (600 * 1024)})
        second = [*prefix, {'role': 'user', 'content': 'The original second question'}]
        status, second_reply = self.request(conversation('unused', messages=second))
        self.assertEqual(status, 200)
        old_branch = [*second, {'role': 'assistant', 'content': second_reply['content']}]
        prior_anchor = self.cloud.history[-1]['transcriptAnchorId']
        actions = []
        events = [{'kind': 'history_truncated', 'sessionId': CLOUD_SESSION, 'anchorId': prior_anchor},
                  {'kind': 'text', 'role': 'assistant', 'sessionId': CLOUD_SESSION, 'content': 'Edited reply.'},
                  {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}]

        def edit_sent(action):
            actions.append(action)
            self.cloud.history[1:] = [cloud_user(action['content'])]

        changed = [*prefix, {'role': 'user', 'content': 'Replace the second question'}]
        payload = conversation('unused', messages=changed)
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(edit_sent, events), OSError)):
            with patch.object(self.manager.cloudcli,'running_sessions',return_value=[{'sessionId':CLOUD_SESSION}]):
                self.assertEqual(self.request(payload, session=str(uuid.uuid4()))[0], 409)
            self.cloud.history.append(cloud_user('Changed from another CloudCLI client'))
            self.assertEqual(self.request(payload)[0], 409)
            self.assertFalse(actions)
            self.cloud.history.pop()
            status, edited = self.request(payload)
            self.assertEqual(status, 200)
            self.assertEqual(self.request(payload), (200, edited))
        self.assertEqual(len(self.cloud.requests), 2)
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0]['type'], actions[0]['sessionId'], actions[0]['anchorId'], actions[0]['content']),
                         ('chat.edit-send', CLOUD_SESSION, prior_anchor, 'Replace the second question'))
        self.assertEqual(self.request(conversation('unused', messages=[*old_branch,
                         {'role': 'user', 'content': 'This stale branch must not resume'}]))[0], 409)
        latest = [*changed, {'role': 'assistant', 'content': edited['content']},
                  {'role': 'user', 'content': 'Continue from the replacement.'}]
        self.assertEqual(self.request(conversation('unused', messages=latest))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        self.assertEqual(self.cloud.requests[-1][2]['message'], 'Continue from the replacement.')

        first_anchor = self.cloud.history[0]['transcriptAnchorId']
        events[0]['anchorId'] = first_anchor

        def replace_first(action):
            actions.append(action)
            self.cloud.history[:] = [cloud_user(action['content'])]

        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(replace_first, events), OSError)):
            self.assertEqual(self.request(conversation('Replace the first question'))[0], 200)
        self.assertEqual(actions[-1]['anchorId'], first_anchor)
        self.assertTrue(actions[-1]['content'].startswith('Replace the first question\n\n本机是'))
        self.assertIn('/Users/example/Work', actions[-1]['content'])

    def test_desktop_one_time_permission_confirms_the_original_remote_operation(self):
        self.config['desktopToolApprovals'] = True
        self.enable_cloudcli_web_fixture()
        tool_input = {'command': 'printf fixture-only', 'description': 'Mock terminal operation'}
        pending = {'requestId': 'fixture-tool-permission', 'sessionId': CLOUD_SESSION,
                   'toolName': 'Bash', 'input': tool_input}
        commands = []

        def cloud_events():
            yield {'kind': 'permission_request', **pending}
            if any(item['type'] == 'chat.permission-response' for item in commands):
                yield {'kind': 'permission_resolved', 'sessionId': CLOUD_SESSION, 'requestId': pending['requestId']}
                yield {'kind': 'text', 'role': 'assistant', 'sessionId': CLOUD_SESSION, 'content': 'Remote fixture done.'}
                yield {'kind': 'complete', 'sessionId': CLOUD_SESSION, 'exitCode': 0}

        def connect(*args, timeout, **kwargs):
            if timeout == 90:
                return ChatSocketFixture(commands.append, cloud_events())
            return ChatSocketFixture(commands.append, [{'kind': 'chat_subscribed', 'sessionId': CLOUD_SESSION,
                'isProcessing': True, 'pendingPermissions': [pending]}])

        with patch.object(gateway, 'cloudcli_websocket', return_value=(connect, OSError)):
            tools = [{'name': 'AskUserQuestion', 'input_schema': {'type': 'object'}}]
            first = conversation('Request a fictional remote operation.', tools=tools)
            status, offered = self.request(first)
            self.assertEqual(status, 200)
            self.assertEqual(commands[0]['options']['permissionMode'], 'default')
            self.assertIn(tool_input['command'], offered['content'][0]['text'])
            card = offered['content'][-1]
            self.assertEqual(card['name'], 'AskUserQuestion')
            question = card['input']['questions'][0]['question']
            answer = {'type': 'tool_result', 'tool_use_id': card['id']}
            messages = [*first['messages'], {'role': 'assistant', 'content': offered['content']},
                        {'role': 'user', 'content': [answer]}]
            payload = conversation('unused', messages=messages)
            answer['content'] = (f'Your questions have been answered: "{question}"="允许所有操作". '
                                 'You can now continue with these answers in mind.')
            self.assertEqual(self.request(payload)[0], 400)
            self.assertEqual(len(commands), 1)
            answer['content'] = answer['content'].replace('允许所有操作', '允许一次')
            status, completed = self.request(payload)
            self.assertEqual(status, 200)
            self.assertEqual(self.request(payload), (200, completed))
        decisions = [action for action in commands if action['type'] == 'chat.permission-response']
        self.assertEqual(decisions, [{'type': 'chat.permission-response', 'requestId': pending['requestId'],
                                     'allow': True, 'updatedInput': tool_input}])
        self.assertFalse(self.cloud.requests)

    def test_desktop_tool_approval_is_explicit_and_denial_keeps_the_tool_blocked(self):
        original = {'command': 'python3 --version', 'description': 'Read-only fixture'}
        pending = {'requestId': 'fixture-tool-permission', 'sessionId': CLOUD_SESSION,
                   'toolName': 'Bash', 'input': original}
        self.assertIsNone(gateway.CloudCliApproval.read(pending, CLOUD_SESSION, {'AskUserQuestion'}, None))
        self.assertIsNone(gateway.CloudCliApproval.read(pending, CLOUD_SESSION, set(), None, True))
        approval = gateway.CloudCliApproval.read(pending, CLOUD_SESSION, {'AskUserQuestion'}, None, True)
        question = approval.block['input']['questions'][0]['question']
        content = (f'Your questions have been answered: "{question}"="拒绝". '
                   'You can now continue with these answers in mind.')
        for result in ({'content': content}, {'is_error': True, 'content': ''}):
            with self.subTest(result=result):
                decision = approval.decision(result)
                self.assertEqual(decision['requestId'], pending['requestId'])
                self.assertIs(decision['allow'], False)
                self.assertNotIn('updatedInput', decision)


if __name__ == '__main__':
    unittest.main()
