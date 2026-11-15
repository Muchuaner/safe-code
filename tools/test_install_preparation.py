"""检查更新阶段与下载错误分类，避免依赖真实网络或修改本机软件包。"""
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Installation(unittest.TestCase):
    def test_dns_error_is_chinese_and_has_stable_code(self):
        p=subprocess.CompletedProcess([],6,'','curl: (6) Could not resolve host: claude.ai\n'*4)
        with patch.object(a.subprocess,'run',return_value=p):
            with self.assertRaises(a.OperationError) as error:a.run(['curl','-fsSL','https://claude.ai/install.sh'])
        self.assertEqual(error.exception.code,'dns_failure');self.assertIn('claude.ai',str(error.exception));self.assertIn('DNS 域名解析失败',str(error.exception));self.assertNotIn('curl:',str(error.exception))
    def test_download_denial_is_not_mislabeled_as_dns(self):
        with patch.object(a.subprocess,'run',return_value=subprocess.CompletedProcess([],22,'','curl: (22) 403')):
            with self.assertRaises(a.OperationError) as error:a.run(['curl','https://claude.ai/install.sh'])
        self.assertEqual(error.exception.code,'package_fetch_failed')
    def test_updates_finish_before_marker_and_repeat_is_skipped(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'STATE',Path(d)),patch.object(a,'log'),patch.object(a,'wait_package_manager') as wait,patch.object(a,'run',return_value=Mock(stdout='')),patch.object(a,'stream') as stream:
            a.prepare_system();wait.assert_called_once();self.assertEqual([x.args[0][-1] if 'upgrade' not in x.args[0] else 'upgrade' for x in stream.call_args_list],['update','upgrade'])
            self.assertTrue((Path(d)/'system-prepared.json').exists());a.prepare_system();self.assertEqual(stream.call_count,2)
    def test_update_failure_never_marks_preparation_done(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'STATE',Path(d)),patch.object(a,'log'),patch.object(a,'wait_package_manager'),patch.object(a,'run',return_value=Mock(stdout='')),patch.object(a,'stream',side_effect=a.OperationError('dns_failure','DNS')):
            with self.assertRaises(a.OperationError):a.prepare_system()
            self.assertFalse((Path(d)/'system-prepared.json').exists())
    def test_background_job_keeps_its_own_verified_payload(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();scripts=root/'scripts';scripts.mkdir();(scripts/'agent.py').write_text('# fixture');(scripts/'network.py').write_text('# fixture')
            name='claude-payload-'+'a'*32;payload=scripts/name;payload.write_bytes(b'fixture binary')
            with patch.object(a,'STATE',root/'state'),patch.object(a,'__file__',str(scripts/'agent.py')),patch.object(a,'run'):
                result=a.start_job({'op':'claude','options':{'claudePayload':str(payload)}})
            directory=root/'state/jobs'/result['jobId'];request=json.loads((directory/'request.json').read_text())
            self.assertEqual(request['options']['claudePayload'],str(directory/name));self.assertEqual((directory/name).read_bytes(),b'fixture binary')
    def test_claude_bad_payload_is_never_installed(self):
        with patch.object(a,'account',return_value={'username':'fixture'}),patch.object(a,'apt'),patch.object(a,'user_files') as install:
            with self.assertRaises(RuntimeError):a.setup_claude({'claudePayload':'/tmp/wrong'})
            install.assert_not_called()
if __name__=='__main__':unittest.main()
