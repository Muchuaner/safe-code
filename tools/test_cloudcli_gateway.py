"""Run the Desktop Messages gateway against CloudCLI-compatible loopback fixtures."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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
    def do_POST(self):
        server = self.server
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        server.requests.append((self.path, self.headers.get('x-api-key'), data))
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
            self.wfile.write(frame)
            self.wfile.flush()
            if getattr(server, 'pause_after_frame', None) == index:
                server.release_stream.wait(5)

    def log_message(self, *_):
        pass


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

    def request(self, payload=None, *, path='/v1/messages', token=DESKTOP_TOKEN, session=None, diagnostic=None):
        headers = {'Authorization': f'Bearer {token}', 'x-claude-code-session-id': session or self.session}
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
            {'kind': 'thinking', 'content': 'raw reasoning is not a signed Messages block'},
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
                self.assertNotIn('raw reasoning', prefix)
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
        self.assertNoNativeFallback()

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
        self.assertTrue(prompt.endswith('Task: Identify this machine.'))
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


if __name__ == '__main__':
    unittest.main()
