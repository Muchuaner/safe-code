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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / 'src/SafeCode.Core/Remote'
PUBLIC = ROOT / 'tools/desktop-api'
sys.path[:0] = [str(REMOTE), str(PUBLIC)]
import desktop_gateway as gateway

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
            self.send_json({'data': {'messages': self.server.history}})
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
        payload = json.dumps({'data': {'models': {'OPTIONS': self.server.model_options}}}).encode()
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


class ChatSocketFixture:
    def __init__(self, send, events=()):
        self.on_send = send
        self.events = iter(events)

    def send(self, raw):
        self.on_send(json.loads(raw))

    def recv(self):
        event = next(self.events, None)
        return json.dumps(event) if event is not None else ''

    def close(self):
        pass


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
        self.cloud.uploads = []
        self.cloud.created_sessions = []
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
        self.assertEqual(task['message'], 'Summarize the example.')
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
            {'kind': 'tool_use', 'toolName': 'Bash', 'toolId': 'upstream-tool'},
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
                prefix = ''.join(event['delta']['text'] for event in decoded if event['type'] == 'content_block_delta')
                self.assertIn('正在思考', prefix)
                self.assertIn('正在运行 `Bash`', prefix)
                self.assertIn('fixture command output', prefix)
                self.assertIn('Reasoning excerpt from the fixture', prefix)
                self.assertTrue(all(event['delta']['type'] == 'text_delta' for event in decoded
                                    if event['type'] == 'content_block_delta'))
            finally:
                self.cloud.release_stream.set()
            tail = [json.loads(line[6:]) for line in response.read().splitlines() if line.startswith(b'data: ')]
        visible = prefix + ''.join(event['delta']['text'] for event in tail if event['type'] == 'content_block_delta')
        self.assertEqual(tail[-1]['type'], 'message_stop')
        self.assertTrue(visible.endswith('Fixture reply'))
        self.assertEqual(visible.count('Fixture reply'), 1)
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
            {'value': 'claude-sonnet-4-6', 'label': 'Outside the gateway allowlist'},
        ]
        self.save_config()
        self.start_gateway()
        status, models = self.request(path='/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual(len(models['data']), 1)
        self.assertEqual(models['data'][0]['display_name'], 'Fixture Haiku')
        self.assertEqual(models['data'][0]['supported_effort_levels'], ['low', 'medium'])
        self.assertEqual(self.request(path='/v1/models'), (200, models))
        self.assertFalse(self.cloud.requests)
        self.assertEqual(self.cloud.model_requests, ['Bearer fixture.web.token'])
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
                self.assertIn('Working directory: '+expected, task['message'])
            else:
                self.assertNotIn('Working directory:', task['message'])
                self.assertNotEqual(task['projectPath'], str(self.project))
                self.assertFalse((Path(task['projectPath'])/'.mcp.json').exists())
            if cwd == desktop:
                self.assertEqual(task['projectPath'], str(self.project))
                messages = [*payload['messages'], {'role': 'assistant', 'content': response['content']},
                            {'role': 'user', 'content': 'Follow up'}]
                self.assertEqual(self.request(conversation('unused', messages=messages))[0], 200)
                self.assertIn(desktop, self.cloud.requests[-1][2]['message'])

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
        self.assertEqual(self.request(payload)[0], 200)
        prompt = self.cloud.requests[-1][2]['message']
        self.assertIn(f'`ssh -F {ssh_config} desk-mac`', prompt)
        self.assertIn('Working directory: '+workspace, prompt)
        self.assertNotIn('host tools', prompt)
        self.assertTrue(prompt.startswith('Identify this machine.\n\nSafeCode workspace context:'))
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
        self.session = str(uuid.uuid4())
        outside = conversation('Example.', system=desktop_environment('/Users/example/Other'))
        self.assertEqual(self.request(outside)[0], 200)
        self.assertNotIn('ssh -F', self.cloud.requests[-1][2]['message'])
        self.session = str(uuid.uuid4())
        ssh_config.write_text('# Safe Code host bridge '+CLIENT_ID+'\nHost desk-mac other\n')
        self.assertEqual(self.request(payload)[0], 409)
        self.assertEqual(len(self.cloud.requests), 3)
        self.assertNoNativeFallback()

    def test_public_cloudcli_backend_requires_loopback_before_listening(self):
        with patch.object(public_gateway, 'load_cloudcli_config', return_value=self.config):
            with self.assertRaisesRegex(ValueError, 'requires a loopback listener'):
                public_gateway.PublicServer(self.working_state, {'bind': '0.0.0.0', 'port': 0})
        self.assertFalse(self.cloud.requests)
        self.assertNoNativeFallback()

    def test_new_public_account_stays_in_native_mode_until_restart(self):
        public_state = self.home / 'public-state'
        (public_state / 'keys').mkdir(parents=True)
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
            '-nodes', '-keyout', str(public_state / 'key.pem'), '-out', str(public_state / 'cert.pem'),
            '-days', '1', '-subj', '/CN=SafeCode Test Fixture',
        ], check=True, capture_output=True, timeout=5)

        # A native listener was started before a later CloudCLI configuration was saved.
        configured = self.config_path.read_text()
        self.config_path.unlink()
        with patch.object(public_gateway, 'load_cloudcli_config', return_value=None) as startup_config:
            with public_gateway.PublicServer(public_state, {'bind': '127.0.0.1', 'port': 0}) as public:
                self.config_path.write_text(configured)
                self.config_path.chmod(0o600)
                self.assertIsNotNone(gateway.load_cloudcli_config())
                account = public.account('f' * 64, 'macos')
                try:
                    self.assertIsNone(account.cloudcli)
                    startup_config.assert_called_once_with()
                finally:
                    account.close()
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
        self.cloud.pause_after_frame = 2
        self.cloud.release_stream = threading.Event()
        self.cloud.reply = (200, [
            {'type': 'session-id', 'sessionId': CLOUD_SESSION},
            {'kind': 'tool_use', 'toolName': 'AskUserQuestion', 'toolId': 'fixture-tool', 'toolInput': questions},
            {'kind': 'permission_request', **pending},
            {'kind': 'permission_resolved', 'requestId': pending['requestId']},
            {'kind': 'text', 'role': 'assistant', 'content': 'Fixture selections accepted.'},
            {'kind': 'complete', 'exitCode': 0}, {'type': 'done'},
        ])
        actions = []
        release = self.cloud.release_stream
        self.addCleanup(release.set)

        class Socket:
            def send(self, raw):
                command = json.loads(raw)
                actions.append(command)
                if command['type'] == 'chat.permission-response':
                    release.set()

            def recv(self):
                return json.dumps({'kind': 'chat_subscribed', 'sessionId': CLOUD_SESSION, 'isProcessing': True,
                                   'pendingPermissions': [pending]})

            def close(self):
                pass

        with patch.object(gateway, 'cloudcli_websocket', return_value=(lambda *a, **kw: Socket(), OSError)):
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
            self.assertFalse(release.is_set())
            self.assertEqual(self.request(first), (200, events))
            self.assertEqual(self.cloud.logins, [credentials])
            rendered = (f'Your questions have been answered: "{question}"="Blue, Green", '
                        f'"{second}"="Custom, with quotes \"yes\"". You can now continue with these answers in mind.'
                        '\n\n<system-reminder>filtered by the gateway</system-reminder>')
            result = {'type': 'tool_result', 'tool_use_id': card['id'], 'content': rendered}
            follow_messages = [*first['messages'], {'role': 'assistant', 'content': content},
                               {'role': 'user', 'content': [result]}]
            second_request = conversation('unused', messages=follow_messages, tools=tools)
            self.assertEqual(self.request(second_request, session=str(uuid.uuid4()))[0], 409)
            self.assertEqual(self.request(conversation('Do something different.'))[0], 409)
            self.assertEqual(len(self.cloud.requests), 1)
            self.assertFalse(actions)
            status, reply = self.request(second_request)
            self.assertEqual(status, 200)
            self.assertIn('Fixture selections accepted.', reply['content'][0]['text'])
            self.assertEqual(self.request(second_request), (200, reply))
            self.assertEqual(len(self.cloud.requests), 1)
            self.assertEqual(actions, [
                {'type': 'chat.subscribe', 'sessions': [{'sessionId': CLOUD_SESSION}]},
                {'type': 'chat.permission-response', 'allow': True, 'requestId': pending['requestId'],
                 'updatedInput': {**questions, 'answers': {question: 'Blue, Green', second: 'Custom, with quotes "yes"'}}},
            ])
            self.cloud.pause_after_frame = None
            self.cloud.reply = (200, cloud_turn('Fixture follow-up.'))
            follow_up = conversation('unused', messages=[*follow_messages, {'role': 'assistant', 'content': reply['content']},
                                                        {'role': 'user', 'content': 'Continue in the same chat.'}], tools=tools)
            self.assertEqual(self.request(follow_up)[0], 200)
            self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
        self.assertNoNativeFallback()

    def test_status_tool_output_subagent_and_context_are_visible_without_streaming(self):
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
            ])},
            {'kind': 'stream_delta', 'parentToolUseId': 'parent', 'content': 'Sample agent answer'},
            {'kind': 'text', 'role': 'assistant', 'parentToolUseId': 'parent', 'content': 'Sample agent answer'},
            {'kind': 'stream_delta', 'parentToolUseId': 'parent', 'content': 'Another partial child reply'},
            {'kind': 'task_status', 'taskId': 'workflow-fixture', 'event': 'progress',
             'agents': [{'label': 'fixture agent', 'state': 'running', 'lastToolName': 'Read'}]},
            {'kind': 'task_notification', 'status': 'completed', 'summary': 'Sample workflow finished'},
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
        self.assertEqual(visible.count('Sample agent answer'), 1)
        for expected in ('Earlier context was trimmed.', 'Provider setup complete', '/compact', 'Compaction started',
                         'Command output', 'Sample subagent thought', 'sample.txt', 'Sample file output',
                         'image 内容可在 CloudCLI 原会话查看', 'Another partial child reply',
                         'fixture agent', 'Sample workflow finished', '125 / 1,000', 'different_new_event'):
            self.assertIn(expected, visible)
        self.assertNotIn('Fixture opaque payload', visible)
        self.assertNotIn('opaque-fixture-image', visible)
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
        payload = conversation('unused', messages=[{'role': 'user', 'content': content}])
        with patch.object(gateway, 'cloudcli_websocket', return_value=(
                lambda *a, **kw: ChatSocketFixture(actions.append, events), OSError)):
            status, reply = self.request(payload)
            self.assertEqual(status, 200)
            self.assertIn('Fixture attachments received.', reply['content'][0]['text'])
            self.assertEqual(self.request(payload), (200, reply))
            self.assertEqual(len(self.cloud.created_sessions), 1)
            self.assertEqual(self.cloud.registered_projects, [str(self.project)])
            self.assertEqual(self.cloud.uploads, [('attachment-1.png', 'image/png', screenshot),
                                                  ('Report.pdf', 'application/pdf', pdf)])
            self.assertEqual(len(actions), 1)
            command = actions[0]
            self.assertEqual((command['type'], command['sessionId'], command['content']),
                             ('chat.send', CLOUD_SESSION, 'Inspect these fixtures.'))
            self.assertEqual([a['path'] for a in command['options']['attachments']],
                             ['/example/assets/0-attachment-1.png', '/example/assets/1-Report.pdf'])
            self.assertFalse(self.cloud.requests)
            follow_up = [*payload['messages'], {'role': 'assistant', 'content': reply['content']},
                         {'role': 'user', 'content': 'Continue in the same conversation.'}]
            self.assertEqual(self.request(conversation('unused', messages=follow_up))[0], 200)
            self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)
            content[-1]['source'] = {'type': 'url', 'url': 'https://example.com/report.pdf'}
            self.assertEqual(self.request(payload, session=str(uuid.uuid4()))[0], 400)
            self.assertEqual(len(actions), 1)

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
            self.assertEqual(actions[0]['content'], 'Prepare a plan.')
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
        following = [*original['messages'], {'role': 'user', 'content': 'Continue after stopping.'}]
        self.assertEqual(self.request(conversation('unused', messages=following))[0], 200)
        self.assertEqual(self.cloud.requests[-1][2]['sessionId'], CLOUD_SESSION)

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
        self.cloud.track_history = True
        first = conversation('The original first question')
        status, reply = self.request(first)
        self.assertEqual(status, 200)
        prefix = [*first['messages'], {'role': 'assistant', 'content': reply['content']}]
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


if __name__ == '__main__':
    unittest.main()
