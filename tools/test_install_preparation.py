"""检查更新阶段与下载错误分类，避免依赖真实网络或修改本机软件包。"""
import hashlib,importlib.util,io,json,socket,subprocess,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch,Mock
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Installation(unittest.TestCase):
    def test_hostname_failure_never_changes_existing_password(self):
        previous={'username':'workspace','password':'old-fixture','hostname':'old-host'}
        calls=[]
        def run(args,*positional,**kwargs):
            calls.append(args)
            if args[0]=='hostnamectl':raise RuntimeError('fixture hostname failure')
            return Mock(returncode=0)
        with patch.object(a,'read_config',return_value=previous),patch.object(a,'run',side_effect=run),patch.object(a,'write') as save:
            with self.assertRaisesRegex(RuntimeError,'hostname failure'):a.setup_account({'password':'new-fixture-password','hostname':'new-host'})
            self.assertFalse(any(c[0]=='chpasswd' for c in calls));save.assert_not_called()
    def test_password_changed_but_record_failed_is_reported_explicitly(self):
        previous={'username':'workspace','password':'old-fixture','hostname':'old-host'}
        with patch.object(a,'read_config',return_value=previous),patch.object(a,'run',return_value=Mock(returncode=0)) as run,patch.object(a,'write',side_effect=OSError('fixture full disk')):
            with self.assertRaisesRegex(RuntimeError,'密码已更新，但账户记录保存失败'):a.setup_account({'password':'new-fixture-password','hostname':'new-host'})
            calls=[c.args[0][0] for c in run.call_args_list];self.assertLess(calls.index('hostnamectl'),calls.index('chpasswd'))
    def claude_download(self,folder,content=b'official fixture',digest=None):
        digest=digest or hashlib.sha256(content).hexdigest()
        urls=[]
        def fetch(url,timeout):
            urls.append(url)
            if url.endswith('/stable'):return io.BytesIO(b'2.1.311\n')
            if url.endswith('/manifest.json'):return io.BytesIO(json.dumps({'platforms':{'linux-arm64':{'checksum':digest}}}).encode())
            return io.BytesIO(content)
        with patch.object(a,'__file__',str(Path(folder)/'agent.py')),patch.object(a,'log'),patch('platform.machine',return_value='aarch64'),patch('urllib.request.urlopen',side_effect=fetch):
            value=a.download_claude()
        self.assertEqual(urls, ['https://downloads.claude.ai/claude-code-releases/stable','https://downloads.claude.ai/claude-code-releases/2.1.311/manifest.json','https://downloads.claude.ai/claude-code-releases/2.1.311/linux-arm64/claude'])
        return value
    def test_vps_download_uses_vps_architecture_and_checks_official_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            result=self.claude_download(folder);target=Path(result['claudePayload'])
            self.assertEqual(target.read_bytes(),b'official fixture');self.assertEqual(target.stat().st_mode&0o777,0o600)
            self.assertEqual(result['claudeVersion'],'2.1.311')
    def test_vps_download_bad_digest_removes_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError,'校验失败'):self.claude_download(folder,digest='0'*64)
            self.assertEqual(list(Path(folder).iterdir()),[])
    def test_vps_dns_failure_has_stable_fallback_code(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(a,'__file__',str(Path(folder)/'agent.py')),patch.object(a,'log'),patch('platform.machine',return_value='x86_64'),patch('urllib.request.urlopen',side_effect=urllib.error.URLError(socket.gaierror('fixture'))):
            with self.assertRaises(a.OperationError) as error:a.download_claude()
            self.assertEqual(error.exception.code,'dns_failure');self.assertIn('VPS',str(error.exception))
            self.assertEqual(list(Path(folder).iterdir()),[])
    def test_vps_download_stream_failure_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(a,'__file__',str(Path(folder)/'agent.py')),patch.object(a,'log'),patch('platform.machine',return_value='x86_64'):
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.side_effect=[b'partial',TimeoutError()]
            manifest=json.dumps({'platforms':{'linux-x64':{'checksum':'0'*64}}}).encode()
            with patch('urllib.request.urlopen',side_effect=[io.BytesIO(b'2.1.311'),io.BytesIO(manifest),response]):
                with self.assertRaises(a.OperationError) as error:a.download_claude()
            self.assertEqual(error.exception.code,'timeout');self.assertEqual(list(Path(folder).iterdir()),[])
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
