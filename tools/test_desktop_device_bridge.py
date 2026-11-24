import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('device_bridge_fixture', ROOT/'tools/desktop-api/device_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class DevicePairingTests(unittest.TestCase):
    def test_key_can_use_only_its_verified_ssh_device_and_stops_when_identity_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner, other, client = 'a'*64, 'b'*64, 'c'*20
            (root/'keys').mkdir()
            for key in (owner, other):
                (root/'keys'/key).touch()
            tunnel = root/'.ssh/safe-code-host'/client
            tunnel.mkdir(parents=True)
            (tunnel/'config').write_text(f'# Safe Code host bridge {client}\nHost win-fixture\n  HostName 127.0.0.1\n  Port 45300\n')
            public_key = tunnel/'identity.pub'
            public_key.write_text('ssh-ed25519 fixture')
            hosts = bridge.OwnerHosts(root, home=root)
            binding = hosts.bind(owner, client, 'fixture PC')
            device_id, proof = binding['deviceId'], binding['deviceProof']
            result = hosts.chat_binding(owner, 'windows', device_id, proof)
            self.assertEqual((result['hostname'], result['alias']), ('fixture PC', 'win-fixture'))
            self.assertTrue(result['command'].endswith(' win-fixture'))
            mac_result = hosts.chat_binding(owner, 'macos', device_id, proof)
            self.assertEqual(mac_result['hostname'], 'fixture PC')
            for key, platform, token in ((other,'windows',proof), (owner,'windows','x'*43), (owner,'macos','x'*43)):
                with self.assertRaises(bridge.DeviceError):
                    hosts.chat_binding(key, platform, device_id, token)
            public_key.write_text('ssh-ed25519 replacement')
            with self.assertRaises(bridge.DeviceError):
                hosts.chat_binding(owner, 'windows', device_id, proof)
            replacement = hosts.bind(owner, client, 'fixture PC')
            with self.assertRaises(bridge.DeviceError):
                hosts.revoke(owner, 'windows', device_id, proof)
            hosts.revoke(owner, 'windows', device_id, replacement['deviceProof'])
            with self.assertRaises(bridge.DeviceError):
                hosts.chat_binding(owner, 'windows', device_id, replacement['deviceProof'])

    def test_two_keys_have_distinct_ports_aliases_and_proofs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner, other = 'a'*64, 'b'*64
            for subdir in ('keys', 'device-access', 'device-required'):
                (root/subdir).mkdir()
                for key in (owner, other):
                    (root/subdir/key).touch()
            devices = bridge.Devices(root, home=root)
            self.assertTrue(devices.required(owner))
            self.assertFalse(devices.required('c'*64))
            first = devices.prepare(owner, 'windows', 'Windows')
            second = devices.prepare(other, 'windows', 'Windows')
            for field in ('deviceId', 'deviceProof', 'alias', 'port', 'publicKey'):
                self.assertNotEqual(first[field], second[field])
            self.assertTrue(first['alias'].startswith('win-'))
            row = devices.enrolled(owner, 'windows', first['deviceId'], first['deviceProof'])
            with self.assertRaises(bridge.DeviceError):
                devices.enrolled(owner, 'windows', second['deviceId'], first['deviceProof'])
            with self.assertRaises(bridge.DeviceError):
                devices.enrolled(owner, 'macos', first['deviceId'], first['deviceProof'])

            user = {'metas': {'device': first['deviceId'], 'proof': first['deviceProof']}}
            self.assertTrue(devices.permit_frp({'op': 'Login', 'content': user}))
            proxy = {'user': user, 'proxy_name': first['alias'], 'proxy_type': 'tcp', 'remote_port': first['port']}
            self.assertTrue(devices.permit_frp({'op': 'NewProxy', 'content': proxy}))
            self.assertFalse(devices.permit_frp({'op': 'NewProxy', 'content': {**proxy, 'remote_port': second['port']}}))
            self.assertFalse(devices.permit_frp({'op': 'NewProxy', 'content': {**proxy, 'proxy_type': 'http'}}))
            self.assertFalse(devices.permit_frp({'op': 'NewProxy', 'content': {**proxy, 'group': 'shared'}}))
            devices.revoke(row)
            self.assertFalse(devices.permit_frp({'op': 'Login', 'content': user}))
            self.assertFalse(devices.ssh_folder(first['deviceId']).exists())
            (root/'keys'/other).unlink()
            self.assertFalse(devices.permit_frp({'op': 'Login', 'content': {
                'metas': {'device': second['deviceId'], 'proof': second['deviceProof']},
            }}))

    def test_shared_key_allows_only_one_device_and_can_be_bound_after_unpairing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner = 'd'*64
            for subdir in ('keys', 'device-access'):
                (root/subdir).mkdir()
                (root/subdir/owner).touch()
            devices = bridge.Devices(root, home=root)
            def pair(name):
                try:
                    return devices.prepare(owner, 'macos', name)
                except bridge.DeviceAlreadyBound:
                    return None

            with ThreadPoolExecutor(2) as work:
                results = list(work.map(pair, ('Mac One', 'Mac Two')))
            self.assertEqual(sum(device is not None for device in results), 1)
            first = next(device for device in results if device is not None)
            row = devices.enrolled(owner, 'macos', first['deviceId'], first['deviceProof'])
            with self.assertRaises(bridge.DeviceAlreadyBound):
                devices.prepare(owner, 'windows', 'Another Host')
            devices.revoke(row)
            replacement = devices.prepare(owner, 'windows', 'Replacement')
            self.assertNotEqual(replacement['deviceId'], first['deviceId'])
            with self.assertRaises(bridge.DeviceError):
                devices.enrolled(owner, 'macos', first['deviceId'], first['deviceProof'])

    def test_pending_registrations_expire_without_reusing_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner = 'b'*64
            for subdir in ('keys', 'device-access'):
                (root/subdir).mkdir()
                (root/subdir/owner).touch()
            devices = bridge.Devices(root, home=root)
            with patch.object(bridge.time, 'time', return_value=100):
                first = devices.prepare(owner, 'macos', 'Mac')
            with patch.object(bridge.time, 'time', return_value=300):
                next_pairing = devices.prepare(owner, 'macos', 'Mac')
                self.assertFalse(devices.permit_frp({'op': 'Login', 'content': {
                    'metas': {'device': first['deviceId'], 'proof': first['deviceProof']},
                }}))
            self.assertFalse(devices.ssh_folder(first['deviceId']).exists())
            self.assertNotEqual(first['alias'], next_pairing['alias'])

    def test_ssh_and_frp_cannot_bind_the_same_key_to_different_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner, ssh_id = 'a'*64, 'b'*20
            for name in ('keys', 'device-access'):
                (root/name).mkdir()
                (root/name/owner).touch()
            tunnel = root/'.ssh/safe-code-host'/ssh_id
            tunnel.mkdir(parents=True)
            (tunnel/'config').write_text(f'# Safe Code host bridge {ssh_id}\nHost win-fixture\n  HostName 127.0.0.1\n  Port 45300\n')
            (tunnel/'identity.pub').write_text('ssh-ed25519 fixture')
            hosts = bridge.OwnerHosts(root, home=root)
            devices = bridge.Devices(root, home=root)
            with ThreadPoolExecutor(2) as work:
                attempts = [work.submit(hosts.bind, owner, ssh_id, 'SSH One'),
                            work.submit(devices.prepare, owner, 'windows', 'FRP One')]
                self.assertEqual(sum(isinstance(attempt.exception(), bridge.DeviceAlreadyBound)
                                     for attempt in attempts), 1)
                winner = next(attempt.result() for attempt in attempts if attempt.exception() is None)
            self.assertEqual(bridge.paired_device(root, owner), winner['deviceId'])


if __name__ == '__main__':
    unittest.main()
