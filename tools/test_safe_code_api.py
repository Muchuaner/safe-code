from contextlib import ExitStack
"""Safe Code API management, queue and usage checks using disposable state."""
import base64
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools/desktop-api'), str(ROOT/'src/SafeCode.Core/Remote')]
from claude_usage import UsageUnavailable, read_subscription
from desktop_gateway import GatewayError
from management import KeyRegistry, RequestQueue, send_admin
from pricing import estimate

spec = importlib.util.spec_from_file_location('safe_code_api_server',ROOT/'tools/desktop-api/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
agent_spec = importlib.util.spec_from_file_location('safe_code_agent', ROOT/'src/SafeCode.Core/Remote/agent.py')
agent = importlib.util.module_from_spec(agent_spec)
agent_spec.loader.exec_module(agent)

def decode_new_ticket(ticket):
    assert ticket.startswith('sc2.')
    text = ticket[4:]
    data = base64.urlsafe_b64decode(text + '='*(-len(text)%4))
    body, tag = data[:-32], data[-32:]
    assert hmac.compare_digest(tag, hmac.digest(server.TICKET_AUTH_KEY, b'sc2.' + body, 'sha256'))
    assert b'https://' not in body
    plaintext = subprocess.run(
        ['/usr/bin/openssl', 'enc', '-d', '-aes-256-cbc', '-K', server.TICKET_ENCRYPTION_KEY,
         '-iv', body[:16].hex()],
        input=body[16:], capture_output=True, check=True,
    ).stdout
    return json.loads(plaintext)


class SafeCodeApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'keys').mkdir()
        self.digests = ['a'*64,'b'*64,'c'*64]
        for key in self.digests[:2]:
            (self.root/'keys'/key).touch()
        self.registry = KeyRegistry(self.root)

    def wait_for(self, predicate):
        start=time.monotonic()
        while not predicate():
            if time.monotonic()-start>2:
                self.fail('timed out')
            time.sleep(0.01)

    def test_official_rates_and_historical_migration(self):
        self.assertEqual(estimate('claude-opus-5-5[1m]', [1_000_000]*4, one_hour_writes=500_000),
                         (30_700_000_000, 0))
        self.assertEqual(estimate('claude-haiku-5-5', [100_000, 0, 0, 0]), (10_000_000, 0))
        self.assertEqual(estimate('claude-haiku-5-5', [99_999, 0, 2, 0]), (49_999_600, 0))
        self.assertEqual(estimate('unknown', [1, 2, 3, 4]), (0, 10))
        path = self.root/'legacy'
        path.mkdir()
        (path/'keys').mkdir()
        (path/'keys'/self.digests[0]).touch()
        with sqlite3.connect(path/'gateway.sqlite3') as db:
            db.executescript('''
                CREATE TABLE keys (id TEXT PRIMARY KEY,note TEXT NOT NULL DEFAULT '',rpm INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,budget_nanos INTEGER);
                CREATE TABLE usage (key_id TEXT,model TEXT,requests INTEGER,failures INTEGER,
                    input INTEGER,output INTEGER,cache_read INTEGER,cache_write INTEGER,last_used INTEGER,
                    PRIMARY KEY(key_id,model));
            ''')
            db.execute('INSERT INTO keys(id,note,created) VALUES(?,?,0)', (self.digests[0], '原有密钥'))
            db.execute('INSERT INTO usage VALUES (?,?,1,0,100,10,20,40,0)',
                       (self.digests[0], 'claude-sonnet-5'))
        for _ in range(2):
            registry = KeyRegistry(path)
            self.assertEqual(registry.usage(self.digests[0])[0]['cost_nanos'], 404_000)
            legacy, = registry.list()
            self.assertEqual((legacy['note'], legacy['expires_at'], legacy['allowed_models']), ('原有密钥', None, None))
            self.assertTrue(registry.enabled(legacy['id']))

    def test_legacy_keys_usage_and_revoke(self):
        first, second, _ = self.digests
        self.assertEqual({first,second},{key['id'] for key in self.registry.list()})
        self.registry.update(first,'笔记本',12)
        self.registry.record(first,'claude-test',False,{'input_tokens':80,'output_tokens':3,'cache_read_input_tokens':40})
        self.registry.record(first,'claude-test',True,{'input_tokens':10})
        self.registry.revoke(first)
        next_registry = KeyRegistry(self.root)
        key = next(row for row in next_registry.list() if row['id']==first)
        self.assertEqual((key['note'],key['rpm'],key['requests'],key['enabled']),('笔记本',12,2,False))
        usage = next_registry.usage(first)[0]
        self.assertEqual((usage['input'],usage['output'],usage['cache_read'],usage['failures']),(90,3,40,1))
        self.assertEqual(next_registry.usage(second),[])
        self.assertEqual(key['tokens'],133)
        overview=next_registry.overview()
        self.assertEqual(overview['models'][0]['requests'],2)
        self.assertEqual(overview['stats']['requests'],0)
        self.assertEqual(overview['trend'],[])

    def test_fifo_timeout_frequency_limit_and_revocation(self):
        first,second,third=self.digests
        (self.root/'keys'/third).touch()
        self.registry.register(third)
        queue=RequestQueue(self.registry,max_wait=.55,per_key=1)
        order=[]
        def wait_slot(key):
            try:
                with queue.slot(key):
                    order.append(key)
            except GatewayError as error:
                order.append(error.status)

        with queue.slot(first):
            b=threading.Thread(target=wait_slot,args=(second,));b.start()
            self.wait_for(lambda:queue.snapshot()[1].get(second)==1)
            with self.assertRaises(GatewayError) as full:
                with queue.slot(second): pass
            self.assertEqual(full.exception.status,429)
            c=threading.Thread(target=wait_slot,args=(third,));c.start()
            self.wait_for(lambda:queue.snapshot()[1].get(third)==1)
        b.join(2);c.join(2)
        self.assertEqual(order,[second,third])
        with queue.slot(first):
            b=threading.Thread(target=wait_slot,args=(second,));b.start()
            b.join(2)
            self.assertEqual(order[-1],429)
            c=threading.Thread(target=wait_slot,args=(third,));c.start()
            self.wait_for(lambda:queue.snapshot()[1].get(third)==1)
            self.registry.revoke(third);queue.wake()
            c.join(2)
            self.assertEqual(order[-1],401)
        self.registry.update(first,'limited',1)
        with self.assertRaises(GatewayError) as rate:
            with queue.slot(first): pass
        self.assertEqual(rate.exception.status,429)
        self.assertEqual(queue.snapshot(), ({}, {}, 1))
        with self.assertRaises(ConnectionAbortedError):
            with queue.slot(second,lambda:True):pass
        self.assertEqual(queue.snapshot(), ({}, {}, 1))

    def test_resize_admits_queued_requests_and_drains_active_requests(self):
        first, second, _ = self.digests
        queue = RequestQueue(self.registry, max_wait=3)
        second_active = threading.Event()
        release_second = threading.Event()
        third_active = threading.Event()

        def run_second():
            with queue.slot(second):
                second_active.set()
                release_second.wait(3)

        def run_third():
            with queue.slot(second):
                third_active.set()

        threads = [threading.Thread(target=run_second), threading.Thread(target=run_third)]
        try:
            with queue.slot(first):
                threads[0].start()
                self.wait_for(lambda: queue.snapshot()[1].get(second) == 1)
                queue.resize(2)
                self.assertTrue(second_active.wait(1))
                queue.resize(1)
                self.assertEqual(queue.snapshot(), ({first: 1, second: 1}, {}, 1))
                threads[1].start()
                self.wait_for(lambda: queue.snapshot()[1].get(second) == 1)
            self.assertEqual(queue.snapshot(), ({second: 1}, {second: 1}, 1))
            self.assertFalse(third_active.is_set())
        finally:
            release_second.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(3)
        self.assertTrue(third_active.is_set())
        self.assertEqual(queue.snapshot(), ({}, {}, 1))

    def test_port_configuration_and_failed_restart_rollback(self):
        config_path = self.root/'config.json'
        original = {'bind': '127.0.0.1', 'port': 45143, 'publicOrigin': 'https://fixture.example'}
        config_path.write_text(json.dumps(original))
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            with self.assertRaisesRegex(ValueError, '占用'):
                server.configure_port(self.root, 45143, occupied.getsockname()[1])
        self.assertEqual(json.loads(config_path.read_text()), original)
        with self.assertRaisesRegex(ValueError, '已变更'):
            server.configure_port(self.root, 45142, 45144)

        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            new_port = probe.getsockname()[1]

        def configure(command, **options):
            _, expected, port, *flags = command
            server.configure_port(self.root, int(expected), int(port), check_available=not flags)

        def restart(command, port):
            self.assertEqual(json.loads(config_path.read_text())['port'], port)
            if port == new_port:
                raise RuntimeError('simulated startup failure')
            return {'port': port}

        with (patch.object(agent, 'STATE', self.root),
              patch.object(agent, 'secure_root_directory'),
              patch.object(agent, 'run', side_effect=configure),
              patch.object(agent, 'safe_code_api_admin', return_value={'port': 45143, 'busy': False}),
              patch.object(agent, 'restart_safe_code_api', side_effect=restart) as restarted):
            with self.assertRaisesRegex(RuntimeError, '已恢复原监听端口'):
                agent.change_safe_code_api_port([], {'port': new_port, 'expectedPort': 45143})
            self.assertEqual([call.args[1] for call in restarted.call_args_list], [new_port, 45143])
        self.assertEqual(json.loads(config_path.read_text()), original)

    def test_direct_origin_uses_current_port_and_preserves_custom_forwarding(self):
        settings = {'port': 45144, 'publicIp': None, 'bind': '0.0.0.0'}
        hints = {'sshHost': '8.8.4.4', 'assignedIps': ['10.1.2.3'], 'egressIp': '1.1.1.1'}
        self.assertEqual(agent.gateway_public_origin(settings, hints), 'https://8.8.4.4:45144')
        self.assertEqual(agent.gateway_public_origin(
            settings, {**hints, 'sshHost': 'vps.example', 'resolved': ['8.8.4.4']}),
            'https://8.8.4.4:45144')
        self.assertEqual(agent.gateway_public_origin(
            {**settings, 'bind': '127.0.0.1', 'origin': 'https://forward.example:5443'}, hints),
            'https://forward.example:5443')
        with self.assertRaisesRegex(ValueError, '回环'):
            agent.gateway_public_origin({**settings, 'bind': '127.0.0.1'}, hints)
        with self.assertRaisesRegex(ValueError, '未识别到'):
            agent.gateway_public_origin(settings, {'sshHost': '192.0.2.1'})

    def test_upgrade_client_issues_and_refreshes_tickets_with_an_older_gateway(self):
        app = self.root/'desktop-api'
        app.mkdir()
        (app/'server.py').touch()
        state = self.root/agent.API_STATE
        (state/'keys').mkdir(parents=True)
        key_id = 'c'*64
        (state/'keys'/key_id).touch()
        pem = '-----BEGIN CERTIFICATE-----\nZml4dHVyZQ==\n-----END CERTIFICATE-----\n'
        (state/'cert.pem').write_text(pem)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        entry = SimpleNamespace(pw_uid=1000, pw_name='fixture', pw_dir=str(self.root))
        origin = 'https://8.8.4.4:45144'
        hints = {'sshHost':'8.8.4.4'}
        bind = '0.0.0.0'

        def manage(command, request):
            if request == {'action':'public_status'}:
                return {'enabled':True, 'backend':'cloudcli'}
            if request == {'action':'settings'}:
                return {'origin':'', 'bind':bind, 'port':45144}
            self.assertEqual(request, {'action':'issue', 'note':'fixture', 'origin':origin})
            return {'ticket':'fixture'}

        def work_account(action, relative, **parameters):
            return subprocess.run([sys.executable, '-I', '-c', agent.USER_FILES], text=True, check=True,
                env={'HOME':str(self.root)}, input=json.dumps({'operation':action, 'relative':relative, **parameters}),
                capture_output=True, timeout=5)

        with (patch.object(agent, 'API_DIRECTORY', app),
              patch.object(agent, 'account', return_value={'username':'fixture'}),
              patch.object(agent.pwd, 'getpwnam', return_value=entry),
              patch.object(agent, 'safe_code_api_admin', side_effect=manage),
              patch.object(agent, 'user_files', side_effect=work_account)):
            issued = agent.safe_code_api({'action':'issue', 'hints':hints, 'note':'fixture'})
            self.assertEqual(issued, {'ticket':'fixture'})
            options = {'action':'ticket_endpoint', 'hints':hints, 'keyId':key_id}
            self.assertEqual(agent.safe_code_api(options), {'origin':origin, 'pin':pin})
            check = {'action':'key_host_reuse', 'keyId':key_id, 'pin':pin}
            self.assertEqual(agent.safe_code_api(check), {'allowed':True})
            self.assertEqual(agent.safe_code_api({**check,'pin':'f'*64}), {'allowed':False})
            (state/'device-required').mkdir()
            required = state/'device-required'/key_id
            required.touch()
            self.assertEqual(agent.safe_code_api(check), {'allowed':False})
            required.unlink()
            bind = '127.0.0.1'
            self.assertEqual(agent.safe_code_api(check), {'allowed':False})
            bind = '0.0.0.0'
            (state/'keys'/key_id).unlink()
            self.assertEqual(agent.safe_code_api(check), {'allowed':False})
            with self.assertRaises(subprocess.CalledProcessError):
                agent.safe_code_api(options)
        self.assertFalse((state/'public-origin').exists())

    def test_gateway_chooses_and_saves_next_port_when_default_is_occupied(self):
        current = {'bind': '0.0.0.0', 'port':45143}
        (self.root/'config.json').write_text(json.dumps(current))
        busy = OSError(errno.EADDRINUSE, 'occupied')
        chosen = SimpleNamespace(server_port=45144)
        with patch.object(server, 'PublicServer', side_effect=[busy, chosen]) as listen:
            self.assertIs(server.open_public_server(self.root, current), chosen)
            self.assertEqual([call.args[1]['port'] for call in listen.call_args_list], [45143, 45144])
        saved = json.loads((self.root/'config.json').read_text())
        self.assertEqual(saved, {'bind':'0.0.0.0', 'port':45144, 'autoPort':45144})
        (self.root/'public-origin').write_text('https://forward.example')
        with patch.object(server, 'PublicServer', side_effect=busy) as listen:
            with self.assertRaises(OSError):
                server.open_public_server(self.root, saved)
            listen.assert_called_once()

    def test_claude_usage_reads_interactive_slash_command_only(self):
        cli = self.root/'.local/bin/claude'
        cli.parent.mkdir(parents=True)
        common = fr"""#!{sys.executable}
import json, os, sys, tty
if sys.argv[1:] == ['auth', 'status', '--json']:
    print(json.dumps({{'loggedIn': True, 'authMethod': 'claude.ai'}}))
    sys.exit(0)
assert '--safe-mode' in sys.argv and '-p' not in sys.argv
assert sys.stdin.isatty() and sys.stdout.isatty()
tty.setraw(sys.stdin.fileno())
print('Accessing workspace:', os.getcwd(), '\r\r\n❯\x1b[4GNo,\x1b[8Gexit\r\r\n\x1b[4GYes,\x1b[9GI\x1b[11Gtrust\x1b[17Gthis\x1b[22Gfolder', flush=True)
assert sys.stdin.buffer.read(3) == b'\x1b[B'
print('\n❯\x1b[4GYes,\x1b[9GI\x1b[11Gtrust\x1b[17Gthis\x1b[22Gfolder', flush=True)
assert sys.stdin.buffer.read(1) == b'\r'
print('\n❯ Try asking a question', flush=True)
assert sys.stdin.buffer.read(6) == b'/usage'
"""
        cli.write_text(common+r"""
print('\n/usage  Show plan usage and limits', flush=True)
assert sys.stdin.buffer.read(1) == b'\r'
print('\nCurrent session\n\x1b[32m42% used\x1b[0m\nCurrent week (all models)\n81% remaining\n', flush=True)
""")
        cli.chmod(0o700)
        with patch.dict(os.environ, {'HOME':str(self.root)}, clear=True):
            self.assertEqual(read_subscription(timeout=3),
                             {'five_hour': {'used':42.0}, 'seven_day': {'used':19.0}})
            cli.write_text(common+r"""
print('\nUsage unavailable', flush=True)
if sys.stdin.buffer.read(1) == b'\r':
    open(os.path.expanduser('~/unexpected-enter'), 'w').close()
""")
            with self.assertRaisesRegex(UsageUnavailable, '内置 /usage'):
                read_subscription(timeout=1.5)
        self.assertFalse((self.root/'unexpected-enter').exists())

    def test_install_checks_cloudcli_and_upgrades_existing_service_without_enabling_public(self):
        app = self.root/'desktop-api'
        unit = self.root/'safe-code-desktop-api.service'
        entry = SimpleNamespace(pw_uid=1000, pw_name='fixture', pw_dir=str(self.root))
        files = {name: base64.b64encode(b'fixture').decode() for name in agent.API_FILES}
        cloud_config = self.root/'.config/safe-code/cloudcli-gateway.json'
        cloud_config.parent.mkdir(parents=True)
        cloud_config.touch()
        start_fails = True
        expected_bind = '127.0.0.1'

        def run(args, **kwargs):
            if args[0] == 'systemctl':
                return SimpleNamespace(stdout='fixture\n' if 'User' in args else 'not-found\n')
            if '--check-cloudcli' in args:
                return SimpleNamespace(returncode=0,stdout=json.dumps({'needsSetup':False}))
            if '--prepare-public-gateway' in args:
                preflight = {'bind':expected_bind} if cloud_config.exists() else {'cloudcliKeyRequired':True}
                return SimpleNamespace(stdout=json.dumps(preflight))
            if args[0] == '/bin/bash':
                self.assertEqual(args[2:], ['fixture', expected_bind, '45143'])
                self.assertEqual(kwargs['env']['SAFECODE_INITIAL_API_INSTALL'], '1')
                self.assertEqual({file.name for file in Path(args[1]).parent.iterdir()}, agent.API_FILES)
                app.mkdir()
                (app/'server.py').touch()
                unit.touch()
                if start_fails:
                    raise RuntimeError('simulated service start failure')
                return SimpleNamespace(stdout='')
            return SimpleNamespace(stdout='true\n')

        with (patch.object(agent, 'STATE', self.root),
              patch.object(agent, 'API_DIRECTORY', app),
              patch.object(agent, 'API_UNIT', unit),
              patch.object(agent, 'NODE_ROOT', self.root/'node'),
              patch.object(agent, 'CODEX_BRIDGE_SOURCE_HASH', hashlib.sha256(b'fixture').hexdigest()),
              patch.object(agent, 'account', return_value={'username': 'fixture'}),
              patch.object(agent.pwd, 'getpwnam', return_value=entry),
              patch.object(agent, 'secure_root_directory'),
              patch.object(agent.os, 'access', return_value=True),
              patch.object(agent.shutil, 'which', return_value='/usr/bin/node'),
              patch.object(agent, 'run', side_effect=run) as runner,
              patch.object(agent, 'safe_code_api_admin', return_value={'enabled': False, 'backend':'cloudcli'})):
            self.assertEqual(agent.safe_code_api({'action':'public_status'}), {'installed':False,'enabled':False,'cloudcliConfigured':True})
            with self.assertRaisesRegex(ValueError, '确认安装'):
                agent.safe_code_api({'action':'install','files':files})
            runner.assert_not_called()
            install = {'action':'install','accepted':True,'files':files}
            with self.assertRaisesRegex(RuntimeError, 'simulated service start failure'):
                agent.safe_code_api(install)
            self.assertFalse(app.exists())
            self.assertFalse(unit.exists())
            self.assertTrue(cloud_config.is_file())
            self.assertTrue(any(call.args[0][1:3]==['disable','--now'] for call in runner.call_args_list))
            start_fails = False
            cloud_config.unlink()
            self.assertEqual(agent.safe_code_api(install), {'cloudcliKeyRequired':True})
            self.assertFalse(app.exists())
            cloud_config.touch()
            self.assertEqual(agent.safe_code_api(install), {'enabled':False, 'backend':'cloudcli'})
            (self.root/'legacy-key-data').write_text('fixture')
            self.assertEqual(agent.safe_code_api(install), {'enabled':False, 'backend':'cloudcli'})
            self.assertEqual((self.root/'legacy-key-data').read_text(), 'fixture')
            self.assertEqual((app/'server.py').read_bytes(), b'fixture')
            self.assertTrue(unit.is_file())

    def test_gateway_provisions_verified_node_without_cloudcli(self):
        node_root = self.root/'node'
        filename = 'node-v22.1.0-linux-x64.tar.gz'
        executable = b'#!/bin/sh\necho true\n'
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
            entry = tarfile.TarInfo('node-v22.1.0-linux-x64/bin/node')
            entry.mode = 0o755
            entry.size = len(executable)
            bundle.addfile(entry, io.BytesIO(executable))
        artifact = archive.getvalue()
        verified = True
        downloads = []

        def run(args, **kwargs):
            if args[0] != 'curl':
                return subprocess.run(args, text=True, capture_output=True, **kwargs)
            if str(args[-1]).endswith('index.json'):
                return SimpleNamespace(stdout=json.dumps([{'version':'v22.1.0','lts':'fixture'}]))
            if str(args[-1]).endswith('SHASUMS256.txt'):
                digest = hashlib.sha256(artifact).hexdigest() if verified else '0'*64
                return SimpleNamespace(stdout=digest+'  '+filename+'\n')
            self.assertTrue(args[-3].endswith(filename))
            Path(args[-1]).write_bytes(artifact)
            downloads.append(args[-3])
            return SimpleNamespace(stdout='')

        with (patch.object(agent, 'STATE', self.root),
              patch.object(agent, 'NODE_ROOT', node_root),
              patch.object(agent.os, 'uname', return_value=SimpleNamespace(machine='x86_64')),
              patch.object(agent.shutil, 'which', side_effect=lambda cmd:'/usr/bin/curl' if cmd=='curl' else None),
              patch.object(agent, 'run', side_effect=run)):
            verified = False
            with self.assertRaisesRegex(RuntimeError, '校验失败'):
                agent.gateway_node()
            self.assertFalse(node_root.exists())
            verified = True
            self.assertEqual(agent.gateway_node(), node_root/'bin/node')
            self.assertEqual(agent.gateway_node(), node_root/'bin/node')
            self.assertEqual(len(downloads), 2)

    def test_failed_gateway_turn_without_final_usage_releases_partial_accounting(self):
        manager = server.PublicManager(self.root, {'instance': 'fixture'}, require_cloudcli=False)
        manager.conversations.append(SimpleNamespace(turn_id='failed-turn', pending={'tool': True}))
        manager.accounting.before = {}
        manager.count_usage({'id': 'completed-piece', 'usage': {'input_tokens': 10}}, 'failed-turn')
        self.assertEqual(manager.take_usage()['input_tokens'], 10)
        self.assertIn('failed-turn', manager.turn_messages)

        manager.accounting.before = {}
        manager.count_usage({'id': 'completed-piece', 'usage': {'input_tokens': 12}}, 'failed-turn')
        manager.record_result('failed-turn', {})
        manager.conversations.clear()
        self.assertEqual(manager.take_usage()['input_tokens'], 2)
        self.assertFalse(manager.usage)
        self.assertFalse(manager.turn_messages)
        self.assertFalse(manager.completed_usage)

        disconnected = SimpleNamespace(turn_id='disconnected-turn', pending={'tool': True})
        manager.conversations.append(disconnected)
        manager.accounting.before = {}
        manager.count_usage({'id': 'known-piece', 'usage': {'input_tokens': 3}}, 'disconnected-turn')
        self.assertEqual(manager.take_usage()['input_tokens'], 3)
        manager.accounting.before = {}
        disconnected.pending.clear()
        manager.finish_conversation_request(disconnected)
        self.assertEqual(manager.accounting.finished['input_tokens'], 0)
        self.assertFalse(manager.turn_messages)
        self.assertFalse(manager.usage)

    def test_concurrent_accounting_preserves_other_turn_usage(self):
        manager = server.PublicManager(self.root, {'instance': 'fixture'}, require_cloudcli=False)
        worker_has_usage = threading.Event()
        finish_worker = threading.Event()
        totals = []

        def count_other_turn():
            manager.accounting.before = {}
            manager.count_usage({'id': 'second-message', 'usage': {'input_tokens': 11}}, 'second-turn')
            manager.record_result('second-turn', {'input_tokens': 11})
            worker_has_usage.set()
            finish_worker.wait(3)
            totals.append(manager.take_usage()['input_tokens'])

        worker = threading.Thread(target=count_other_turn)
        worker.start()
        try:
            self.assertTrue(worker_has_usage.wait(1))
            manager.accounting.before = {}
            manager.count_usage({'id': 'first-message', 'usage': {'input_tokens': 7}}, 'first-turn')
            self.assertEqual(manager.take_usage()['input_tokens'], 7)
        finally:
            finish_worker.set()
            worker.join(3)
        self.assertEqual(totals, [11])

    @patch.object(server, 'load_cloudcli_config', return_value={'fixture': True})
    def test_activation_is_required_after_the_first_active_key(self, _):
        openssl = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=fixture']
        subprocess.run([*openssl, '-keyout', self.root/'key.pem', '-out', self.root/'cert.pem'], check=True, capture_output=True)
        for digest in self.digests[:2]:
            self.registry.revoke(digest)
        with server.PublicServer(self.root, {'bind':'127.0.0.1','port':0,'publicOrigin':'https://fixture.example'}) as public:
            public.manage({'action':'enable_public','accepted':True})
            self.assertFalse(public.manage({'action':'list'})['activation_required'])

            def free_issue():
                try:
                    return public.manage({'action':'issue'})['id']
                except ValueError as error:
                    self.assertIn('激活码', str(error))
                    return None

            with ThreadPoolExecutor(2) as pool:
                issued = [key for key in pool.map(lambda _: free_issue(), range(2)) if key]
            self.assertEqual(len(issued), 1)
            self.assertTrue(public.manage({'action':'list'})['activation_required'])
            for invalid in ('', 'wrong', 'agentom', 123):
                with self.assertRaisesRegex(ValueError, '激活码'):
                    public.manage({'action':'issue','activation_code':invalid})
            destination = self.root/'unissued.key'
            with self.assertRaisesRegex(ValueError, '激活码'):
                server.issue(self.root, 'https://fixture.example', destination)
            self.assertFalse(destination.exists())
            second = public.manage({'action':'issue','activation_code':'Agentom'})['id']
            self.assertEqual({row['id'] for row in self.registry.list() if row['enabled']}, {issued[0], second})
            public.manage({'action':'revoke','id':second})
            public.manage({'action':'revoke','id':issued[0]})
            self.assertFalse(public.manage({'action':'list'})['activation_required'])
            self.assertIsNotNone(free_issue())

    @patch.object(server, 'load_cloudcli_config', return_value={'fixture': True})
    def test_local_admin_and_public_api_boundary(self, _):
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048',
                        '-nodes','-days','1','-subj','/CN=fixture','-keyout',self.root/'key.pem',
                        '-out',self.root/'cert.pem'],check=True,capture_output=True)
        with server.PublicServer(self.root,{'bind':'127.0.0.1','port':0,
                                           'publicOrigin':'https://fixture.example'}) as public:
            fake=server.PublicManager(self.root,{'instance':'fixture'},require_cloudcli=False)
            fake.catalog=lambda: {'data':[{'id':'claude-test'}]}
            def model(data, **options):
                fake.count_usage({'id':'fixture','usage':{'input_tokens':7}}, 'turn')
                fake.record_result('turn',{'input_tokens':7,'output_tokens':2})
                return {'model':'claude-test','usage':{'input_tokens':7,'output_tokens':2}}
            fake.run=model
            public.account=lambda *_:fake
            with socketserver.UnixStreamServer(str(self.root/'admin.sock'),server.AdminHandler) as admin:
                admin.manage=public.manage
                for endpoint in (public,admin):
                    threading.Thread(target=endpoint.serve_forever,daemon=True).start()
                    self.addCleanup(endpoint.shutdown)

                def admin_request(action, **args):
                    return send_admin(self.root,json.dumps({'action':action,**args}).encode())

                def manage(action, **args):
                    if action == 'issue':
                        args.setdefault('activation_code', 'Agentom')
                    return admin_request(action, **args)['value']

                def request(method, path, key=None, body=None):
                    conn=http.client.HTTPSConnection('127.0.0.1',public.server_port,
                                                     context=ssl._create_unverified_context(),timeout=3)
                    try:
                        conn.request(method,path,body,{'Authorization':'Bearer '+key} if key else {})
                        response=conn.getresponse()
                        return response.status,response.read()
                    finally:
                        conn.close()

                self.assertEqual(manage('public_status'), {'enabled': False, 'backend':'cloudcli', 'gatewayRevision':server.GATEWAY_REVISION})
                for operation in ('list', 'settings', 'issue', 'enable_public'):
                    self.assertEqual(admin_request(operation), {'error': server.PUBLIC_DISABLED_MESSAGE})
                self.assertEqual(admin_request('enable_public', accepted='true'), {'error': server.PUBLIC_DISABLED_MESSAGE})
                earlier_ticket, legacy_id = server.make_key(self.root, 'https://fixture.example')
                legacy_key = decode_new_ticket(earlier_ticket)['key']
                self.registry.register(legacy_id)
                (self.root/'keys'/legacy_id).touch()
                self.assertEqual(request('GET','/v1/models?test=1',legacy_key)[0],403)
                self.assertEqual(request('POST','/v1/messages',legacy_key)[0],403)
                counter_path='/v1/messages/count_tokens'
                self.assertEqual(request('POST',counter_path,legacy_key)[0],403)
                with self.assertRaisesRegex(ValueError, '启用公网'):
                    server.issue(self.root, 'https://fixture.example', self.root/'unissued.key')
                self.assertFalse((self.root/'unissued.key').exists())
                self.assertEqual(manage('enable_public', accepted=True), {'enabled': True, 'backend':'cloudcli', 'gatewayRevision':server.GATEWAY_REVISION})
                runtime = manage('public_status', runtime=True)
                self.assertEqual((runtime['enabled'], runtime['active'], runtime['waiting']), (True, 0, 0))
                self.assertEqual(runtime['subscription'], {'state': 'unavailable', 'windows': {}})
                with patch.object(server, 'read_subscription', return_value={'five_hour': {'used': 23.5}}) as lookup:
                    self.assertEqual(manage('list', details=True)['subscription'], runtime['subscription'])
                    lookup.assert_not_called()
                    value = manage('refresh_subscription')
                    self.assertEqual(value['windows'], {'five_hour': {'used': 23.5}})
                    self.assertEqual(manage('public_status', runtime=True)['subscription'], value)
                    lookup.assert_called_once()
                self.assertEqual(runtime['concurrency'], 0)
                with ExitStack() as slots:
                    for _ in range(35):
                        slots.enter_context(public.queue.slot(legacy_id))
                    self.assertEqual(manage('list')['active_count'], 35)
                self.assertEqual(manage('set_concurrency', concurrency=0),
                                 {'concurrency': 0, 'concurrency_text': '0'})
                self.assertEqual(manage('set_concurrency', concurrency='0'),
                                 {'concurrency': 0, 'concurrency_text': '0'})
                for invalid in (True, -5, 2.5, '1e3', '00'):
                    self.assertIn('error', admin_request('set_concurrency', concurrency=invalid))
                self.assertEqual(manage('set_concurrency', concurrency='30'),
                                 {'concurrency': 30, 'concurrency_text': '30'})
                self.assertEqual(manage('list')['concurrency'], 30)
                for capacity in (public.turns, public.webfetch_turns, public.workers):
                    for _ in range(30):
                        self.assertTrue(capacity.acquire(blocking=False))
                    for _ in range(30):
                        capacity.release()
                self.assertEqual(manage('set_concurrency', concurrency=2),
                                 {'concurrency': 2, 'concurrency_text': '2'})
                with public.queue.slot(legacy_id), public.queue.slot(legacy_id):
                    summary = manage('list')
                    self.assertEqual((summary['active_count'], summary['concurrency']), (2, 2))
                    legacy = next(key for key in summary['keys'] if key['id'] == legacy_id)
                    self.assertEqual(legacy['active_count'], 2)
                with server.PublicServer(self.root,{'bind':'127.0.0.1','port':0}) as reopened:
                    self.assertEqual(reopened.manage({'action':'public_status'}), {'enabled':True, 'backend':'cloudcli', 'gatewayRevision':server.GATEWAY_REVISION})
                    self.assertEqual(reopened.manage({'action':'list'})['concurrency'], 2)
                self.assertEqual(request('GET','/v1/cloudcli/models',legacy_key)[0],200)

                settings = manage('settings')
                self.assertEqual(settings, {'bind': '127.0.0.1', 'port': public.server_port,
                                            'publicIp': None, 'origin': 'https://fixture.example',
                                            'busy': False})
                with public.queue.slot(self.digests[0]):
                    self.assertTrue(manage('settings')['busy'])
                public.config['publicIp'] = '8.8.8.8'
                self.assertEqual(manage('settings')['publicIp'], '8.8.8.8')

                def post(path, key=None, body=True):
                    return request('POST',path,key,json.dumps({'model':'claude-test'}) if body else None)

                created=manage('issue',note='设备一',rpm=1)
                ticket_payload = decode_new_ticket(created['ticket'])
                self.assertEqual(ticket_payload['url'], 'https://fixture.example')
                key=ticket_payload['key']
                self.assertEqual(hashlib.sha256(key.encode()).hexdigest(),created['id'])
                self.assertEqual(post('/admin',key,False)[0],404)
                self.assertEqual(request('GET','/v1/key/balance')[0],401)
                status, balance = request('GET','/v1/key/balance',key)
                self.assertEqual(status,200)
                self.assertEqual(json.loads(balance),{'budget_nanos':None,'used_nanos':0,'remaining_nanos':None})
                self.assertEqual(request('GET','/v1/key/balance?id='+created['id'],legacy_key)[0],404)
                self.assertNotIn(key,json.dumps(manage('list')))
                self.assertEqual(post('/v1/cloudcli/messages',key)[0],200)
                self.assertEqual(post('/v1/messages',key)[0],429)
                details=manage('usage',id=created['id'])
                model_usage=details['models'][0]
                self.assertEqual((model_usage['requests'],model_usage['input'],model_usage['output']),(1,7,2))
                limited, success=details['recent']
                self.assertEqual((limited['outcome'],limited['status'],limited['duration_ms']),('limited',429,None))
                self.assertEqual((success['outcome'],success['status'],success['tokens']),('success',200,9))
                self.assertGreaterEqual(success['wait_ms'],0)
                self.assertGreaterEqual(success['duration_ms'],0)
                overview=manage('list',details=True)
                self.assertEqual((overview['stats']['requests'],overview['stats']['tokens'],overview['stats']['limited']),(2,9,1))
                self.assertEqual(overview['trend'][0]['tokens'],9)
                self.assertEqual(overview['recent'],details['recent'])
                self.assertNotIn(key,json.dumps(overview))
                analytics = manage('list', details=True, analytics='1h')['analytics']
                self.assertEqual((analytics['step'], analytics['count']), (300, 12))
                self.assertEqual(sum(point['requests'] for point in analytics['timeline']), 2)
                self.assertEqual(sum(point['tokens'] for point in analytics['timeline']), 9)
                self.assertEqual(sum(point['limited'] for point in analytics['timeline']), 1)
                self.assertEqual(analytics['latency']['duration_ms']['samples'], 1)
                self.assertEqual(analytics['latency']['wait_ms']['samples'], 2)
                self.assertEqual(analytics['latency']['duration_ms']['p95_ms'], success['duration_ms'])
                self.assertNotIn('analytics', manage('list', details=True, analytics=['1h']))
                self.assertIn('请先撤销', admin_request('delete', id=created['id'])['error'])
                self.assertEqual(manage('usage', id=created['id']), details)
                manage('revoke',id=created['id'])
                self.assertEqual(manage('usage', id=created['id']), details)
                self.assertEqual(manage('delete', id=created['id']), {'id': created['id']})
                self.assertEqual(manage('delete', id=created['id']), {'id': created['id']})
                public.registry.record(created['id'], success['model'], False, {'output_tokens': 1}, request=success)
                public.registry.record_unadmitted(created['id'], limited)
                self.assertEqual(manage('usage', id=created['id']), {'models': [], 'recent': []})
                key_ids = {entry['id'] for entry in manage('list')['keys']}
                self.assertNotIn(created['id'], key_ids)
                self.assertIn(legacy_id, key_ids)
                self.assertEqual(manage('list', details=True)['stats']['requests'], 0)
                self.assertEqual(post('/v1/messages',key,False)[0],401)
                self.assertEqual(post(counter_path,key,False)[0],401)
                self.assertEqual(post(counter_path,body=False)[0],401)
                counter=manage('issue',note='Token count only',rpm=1)
                counter_key=decode_new_ticket(counter['ticket'])['key']
                with patch.object(public, 'account', wraps=public.account) as account:
                    for _ in range(2):
                        status, body=post(counter_path+'?beta=true',counter_key)
                        self.assertEqual(status,404)
                        self.assertEqual(json.loads(body)['error']['type'],'not_found_error')
                    account.assert_not_called()
                self.assertEqual(manage('usage',id=counter['id']), {'models': [], 'recent': []})
                self.assertEqual(manage('list',details=True)['stats']['requests'],0)
                self.assertEqual(post('/v1/messages',counter_key)[0],200)
                self.assertEqual(post('/v1/messages',counter_key)[0],429)
                with patch.object(fake, 'run', side_effect=server.GatewayError('Simulated model request error',400)):
                    self.assertEqual(post('/v1/messages',legacy_key)[0],400)
                model_failure = manage('usage',id=legacy_id)['recent'][0]
                self.assertEqual((model_failure['outcome'],model_failure['status']),('failed',400))
                capped = manage('issue', budget_cents=0)
                capped_key = decode_new_ticket(capped['ticket'])['key']
                self.assertEqual(json.loads(request('GET','/v1/key/balance',capped_key)[1])['remaining_nanos'],0)
                self.assertEqual(post('/v1/messages', capped_key)[0], 402)
                manage('update', id=capped['id'], budget_cents=1)
                public.registry.record(capped['id'], 'claude-opus-5-5', False, {'output_tokens': 500})
                manage('update', id=capped['id'], note='保留额度')
                self.assertEqual(post('/v1/messages', capped_key)[0], 402)
                manage('update', id=capped['id'], budget_cents=2)
                self.assertEqual(post('/v1/messages', capped_key)[0], 200)
                self.assertIn('error', admin_request('set_origin', origin='https://fixture.example/path'))
                origin = 'https://new.fixture.example:45143'
                self.assertEqual(manage('set_origin', origin=origin+'/')['origin'], origin)
                self.assertEqual(manage('settings')['origin'], origin)
                self.assertEqual(decode_new_ticket(manage('issue', note='设备二', rpm=10)['ticket'])['url'], origin)
                with server.PublicServer(self.root,{'bind':'127.0.0.1','port':0}) as reopened:
                    self.assertEqual(reopened.manage({'action':'settings'})['origin'], origin)
                admin.shutdown()
            public.shutdown()


if __name__=='__main__':
    unittest.main()
