"""MCP and workspace boundary checks. No network services are started."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

TOOL = Path(__file__).resolve().parent / 'desktop-api/host_control_mcp.py'
SPEC = importlib.util.spec_from_file_location('host_control_mcp', TOOL)
mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp)

CLIENT = 'a' * 20


class McpTests(unittest.TestCase):
    def test_mcp_handshake_and_tool_result(self):
        data = [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'method': 'notifications/initialized'},
            {'id': 2, 'method': 'tools/list'},
        ]
        payload = '\n'.join(json.dumps({'jsonrpc': '2.0', **message}) for message in data) + '\n'
        result = subprocess.run([sys.executable, '-I', '-B', str(TOOL), '--client-id', CLIENT,
                                 '--workspace', '/Users/example/project'], input=payload,
                                capture_output=True, check=True, text=True, timeout=5)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([message['id'] for message in messages], [1, 2])
        self.assertEqual(messages[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual({tool['name'] for tool in messages[1]['result']['tools']}, set(mcp.TOOL_DEFINITIONS))
        with patch.object(mcp, 'call_tool', return_value={'text': 'expected'}) as tool:
            result = mcp.dispatch({'method': 'tools/call', 'params': {
                'name': 'read_text_file', 'arguments': {'path': 'demo.txt'}}}, CLIENT, '/Users/example/project')
        tool.assert_called_once_with(CLIENT, '/Users/example/project', 'read_text_file', {'path': 'demo.txt'})
        self.assertEqual(result, {'content': [{'type': 'text', 'text': 'expected'}]})

    def test_managed_project_checks_live_workspace_binding_before_accessing_mac(self):
        request = {'method': 'tools/call', 'params': {
            'name': 'read_text_file', 'arguments': {'path': 'demo.txt'},
        }}
        workspace = '/Users/example/Work'
        with tempfile.TemporaryDirectory() as folder, patch.object(mcp.Path, 'home', return_value=Path(folder)):
            config = Path(folder)/'.config/safe-code/cloudcli-gateway.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'hostControl': {'clientId': CLIENT, 'workspace': workspace}}))
            config.chmod(0o600)
            with patch.object(mcp, 'call_tool', return_value={'text': 'fixture'}) as tool:
                result = mcp.dispatch(request, CLIENT, workspace+'/Notes', managed_project=True)
                self.assertEqual(result, {'content': [{'type': 'text', 'text': 'fixture'}]})
                tool.assert_called_once_with(CLIENT, workspace+'/Notes', 'read_text_file', {'path': 'demo.txt'})
                tool.reset_mock()
                self.assertTrue(mcp.dispatch(request, CLIENT, workspace+'-Other', managed_project=True)['isError'])
                self.assertTrue(mcp.dispatch(request, 'b'*20, workspace, managed_project=True)['isError'])
                desktop = '/Users/example/Desktop'
                config.write_text(json.dumps({'hostControl': {
                    'clientId': CLIENT, 'workspace': workspace, 'projectWorkspaces': [desktop],
                }}))
                self.assertTrue(mcp.dispatch(request, CLIENT, workspace+'/Notes', managed_project=True)['isError'])
                self.assertFalse(mcp.dispatch(request, CLIENT, desktop, managed_project=True).get('isError', False))
                tool.assert_called_once_with(CLIENT, desktop, 'read_text_file', {'path': 'demo.txt'})
                tool.reset_mock()
                config.unlink()
                self.assertTrue(mcp.dispatch(request, CLIENT, workspace+'/Notes', managed_project=True)['isError'])
                tool.assert_not_called()

    def test_uses_only_generated_loopback_config(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mcp.Path, 'home', return_value=Path(folder)):
            home = Path(folder).resolve()
            config = home / '.ssh/safe-code-host' / CLIENT / 'config'
            config.parent.mkdir(mode=0o700, parents=True)
            known = config.with_name('known_hosts')
            identity = config.with_name('identity')
            pin_name = 'safe-code-host-' + CLIENT
            config.write_text(f'# Safe Code host bridge {CLIENT}\nHost mac\nHostName 127.0.0.1\n'
                              f'Port 49322\nUser mac-user\nIdentityFile {identity}\n'
                              f'UserKnownHostsFile {known}\nHostKeyAlias {pin_name}\n')
            known.write_text(pin_name + ' ssh-ed25519 AAAAfixture\n')
            identity.write_text('placeholder')
            for path in (config, known, identity):
                path.chmod(0o600)
            argv = mcp.bridge_arguments(CLIENT)
            self.assertEqual(argv[0:4], ['/usr/bin/ssh', '-F', '/dev/null', '-T'])
            self.assertEqual(argv[-3:-1], ['--', '127.0.0.1'])
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertIn('IdentityAgent=none', argv)
            config.write_text(config.read_text().replace('127.0.0.1', 'outside.example'))
            with self.assertRaises(mcp.ToolError):
                mcp.bridge_arguments(CLIENT)

    def test_transport_sends_one_request_and_bounds_output(self):
        cmd = [sys.executable, '-c', "import json, sys; print(json.dumps(json.load(sys.stdin)))"]
        self.assertEqual(mcp.exchange_ssh(cmd, b'{"fixture": true}'), {'fixture': True})
        with patch.object(mcp, 'MAX_FRAME', 20), self.assertRaisesRegex(mcp.ToolError, 'response is too large'):
            mcp.exchange_ssh(cmd, b'{"fixture": "a long local response"}')


@unittest.skipUnless(sys.platform == 'darwin', 'Uses macOS exclusive rename and directory semantics')
class MacWorkspaceTests(unittest.TestCase):
    def test_desktop_workspace_reads_only_the_requested_session_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            sessions = root/'Library/Application Support/Claude-3p/claude-code-sessions'
            records = sessions/str(uuid.uuid4())/str(uuid.uuid4())
            records.mkdir(parents=True)
            current, previous = str(uuid.uuid4()), str(uuid.uuid4())
            desktop = str(root/'Desktop')
            metadata = records/('local_'+str(uuid.uuid4())+'.json')
            record = {'cliSessionId': current, 'priorCliSessionIds': [previous],
                      'folderPlace': {'cwd': desktop}, 'originCwd': desktop,
                      'title': 'private fixture title'}
            metadata.write_text(json.dumps(record))

            def resolve(identifier):
                result = subprocess.run([sys.executable, '-I', '-B', '-c', mcp.REMOTE_SCRIPT],
                                        input=json.dumps({'name': 'desktop_workspace', 'session': identifier}),
                                        env={**os.environ, 'HOME': str(root)}, capture_output=True,
                                        check=True, text=True, timeout=5)
                return json.loads(result.stdout)

            self.assertEqual(resolve(current), {'workspace': desktop})
            self.assertEqual(resolve(previous), {'workspace': desktop})
            self.assertEqual(resolve(str(uuid.uuid4())), {})
            other = records/('local_'+str(uuid.uuid4())+'.json')
            other.write_text(json.dumps({**record, 'folderPlace': {'cwd': str(root/'Other')}}))
            self.assertEqual(resolve(current), {})

    def test_file_lifecycle_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve()
            work = directory / 'workspace'
            work.mkdir()
            target = directory / 'outside.txt'
            target.write_text('outside')
            (work / 'out').symlink_to(target)
            (work / 'escape').symlink_to(directory, target_is_directory=True)

            def run(name, **arguments):
                result = subprocess.run([sys.executable, '-I', '-B', '-c', mcp.REMOTE_SCRIPT],
                                        input=json.dumps({'name': name, 'workspace': str(work), 'arguments': arguments}),
                                        capture_output=True, check=True, text=True, timeout=5)
                return json.loads(result.stdout)

            self.assertEqual(run('create_text_file', path='你好.txt', text='内容'), {'created': True})
            self.assertEqual(run('read_text_file', path='你好.txt'), {'text': '内容'})
            self.assertIn('error', run('create_text_file', path='你好.txt', text='replacement'))
            self.assertIn('error', run('read_text_file', path='out'))
            self.assertIn('error', run('read_text_file', path='escape/outside.txt'))
            self.assertIn('error', run('read_text_file', path='../outside.txt'))
            self.assertIn('error', run('move_path', source='你好.txt', destination='out'))
            self.assertEqual(run('move_path', source='你好.txt', destination='moved.txt'), {'moved': True})
            self.assertEqual(run('list_files', path='.')['entries'], [
                {'name': 'escape', 'type': 'symlink'},
                {'name': 'moved.txt', 'type': 'file'},
                {'name': 'out', 'type': 'symlink'},
            ])
            self.assertEqual(target.read_text(), 'outside')


if __name__ == '__main__':
    unittest.main()
