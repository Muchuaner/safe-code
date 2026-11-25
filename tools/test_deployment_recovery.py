"""Exercise the deployment result race and safe input-method removal."""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

module = Path(__file__).resolve().parents[1] / 'src/SafeCode.Core/Remote/agent.py'
spec = importlib.util.spec_from_file_location('agent', module)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class DeploymentRecovery(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.state = Path(folder)
        self.enterContext(patch.object(agent, 'STATE', self.state))
        self.job = 'a' * 32
        self.directory = self.state / 'jobs' / self.job
        self.directory.mkdir(parents=True)
        (self.directory / 'metadata.json').write_text('{"created":1,"operation":"account"}')
        self.inactive = subprocess.CompletedProcess([], 3, 'inactive\n', '')

    def test_result_written_before_unit_exits_wins(self):
        def collect(*args, **kwargs):
            (self.directory / 'result.json').write_text('{"statuses":{"account":true}}')
            return self.inactive

        with patch.object(agent, 'run', side_effect=collect):
            outcome = agent.poll_job(self.job, 0)
        self.assertTrue(outcome['done'])
        self.assertIsNone(outcome['error'])
        self.assertEqual(outcome['value']['statuses'], {'account': True})

    def test_manager_unavailable_or_installer_running_is_not_a_failure(self):
        unavailable = subprocess.CompletedProcess([], 1, '', 'Failed to connect to bus')
        with patch.object(agent, 'run', return_value=unavailable):
            self.assertFalse(agent.poll_job(self.job, 0)['done'])
        with (self.state / 'install.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(agent, 'run', return_value=self.inactive):
                self.assertFalse(agent.poll_job(self.job, 0)['done'])
        with patch.object(agent, 'run', return_value=self.inactive):
            self.assertIn('未写入结果', agent.poll_job(self.job, 0)['error'])

    def test_explicit_job_error_is_preserved(self):
        (self.directory / 'error.json').write_text('{"error":"fixture failure","code":"disk_full"}')
        with patch.object(agent, 'run') as status:
            outcome = agent.poll_job(self.job, 0)
        status.assert_not_called()
        self.assertTrue(outcome['done'])
        self.assertEqual(outcome['errorCode'], 'disk_full')


class InputRemoval(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'workspace'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.owner = {'username': 'workspace', 'uid': 1000}
        self.installed = True
        self.calls = []
        self.enterContext(patch.object(agent, 'STATE', self.state))
        self.enterContext(patch.object(agent, 'pinyin_owner', return_value=self.owner))
        self.enterContext(patch.object(agent, 'user_files', side_effect=self.user_files))
        self.enterContext(patch.object(agent, 'package_installed', side_effect=lambda _: self.installed))
        self.enterContext(patch.object(agent, 'run', side_effect=self.package_remove))
        self.enterContext(patch.object(agent, 'wait_package_manager'))
        self.enterContext(patch.object(agent, 'log'))
        self.enterContext(patch.object(agent.pwd, 'getpwall', return_value=[]))

    def user_files(self, operation, relative, **parameters):
        result = subprocess.run(
            [sys.executable, '-I', '-c', agent.USER_FILES],
            input=json.dumps(dict(operation=operation, relative=relative, **parameters)),
            text=True, capture_output=True, env={**os.environ, 'HOME': str(self.home)},
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def package_remove(self, args, *other, **kwargs):
        self.calls.append(args)
        self.installed = False
        return subprocess.CompletedProcess(args, 0, '', '')

    def create_legacy_config(self):
        self.user_files('write', agent.INPUT_AUTOSTART, content=agent.INPUT_DESKTOP)
        self.user_files('write', '.config/fcitx5/profile', content='user-dictionary-fixture')
        (self.home / '.xsessionrc').write_text('export LANG=ko_KR.UTF-8\n'+agent.INPUT_ENVIRONMENT+'export USER_SETTING=keep\n')

    def test_legacy_removal_keeps_shared_package_profile_and_desktop_settings(self):
        self.create_legacy_config()
        for _ in range(2):
            agent.remove_input({})
        self.assertTrue(self.installed)
        self.assertFalse((self.home / agent.INPUT_AUTOSTART).exists())
        self.assertEqual((self.home / '.config/fcitx5/profile').read_text(), 'user-dictionary-fixture')
        self.assertEqual((self.home / '.xsessionrc').read_text(), 'export LANG=ko_KR.UTF-8\nexport USER_SETTING=keep\n')
        self.assertEqual(self.calls, [])

    def test_managed_package_removed_and_reinstall_preserves_profile(self):
        self.create_legacy_config()
        (self.state / 'input-pinyin-owner.json').write_text(json.dumps(self.owner))
        agent.remove_input({})
        self.assertEqual(self.calls, [['dpkg', '--remove', 'fcitx5-pinyin']])
        with patch.object(agent, 'account'), patch.object(agent, 'apt') as install:
            agent.setup_input({})
        install.assert_called_once_with(agent.INPUT_PACKAGES + ['fonts-noto-cjk'])
        self.assertEqual((self.home / agent.INPUT_AUTOSTART).read_text(), agent.INPUT_DESKTOP)
        self.assertEqual((self.home / '.config/fcitx5/profile').read_text(), 'user-dictionary-fixture')

    def test_fresh_desktop_does_not_enable_fcitx_and_custom_entry_is_preserved(self):
        agent.update_input_environment(False, 'ko_KR.UTF-8')
        self.assertEqual((self.home / '.xsessionrc').read_text(), 'export LANG=ko_KR.UTF-8\n')
        self.user_files('write', agent.INPUT_AUTOSTART, content='custom-input')
        with self.assertRaisesRegex(RuntimeError, '自启动文件已被修改'):
            agent.remove_input({})
        self.assertEqual((self.home / agent.INPUT_AUTOSTART).read_text(), 'custom-input')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
