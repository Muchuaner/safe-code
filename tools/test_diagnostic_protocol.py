"""Remote error codes survive without sending raw output in diagnostics."""
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
class DiagnosticProtocol(unittest.TestCase):
    def test_install_failure_codes_and_success_after_lock(self):
        for line,code in [('Waiting for cache lock: private','package_lock_wait'),('Could not resolve private','dns_failure'),('Failed to fetch private','package_fetch_failed'),('No space left on device','disk_full')]:
            process=type('Process',(),{'stdout':[line],'returncode':100,'wait':lambda self:100})()
            with patch.object(agent.subprocess,'Popen',return_value=process),patch.object(agent,'log'):
                with self.assertRaises(agent.OperationError) as error:agent.stream(['fixture'])
                self.assertEqual(error.exception.code,code);self.assertNotIn('private',str(error.exception))
        process=type('Process',(),{'stdout':['Waiting for cache lock'],'returncode':0,'wait':lambda self:0})()
        with patch.object(agent.subprocess,'Popen',return_value=process),patch.object(agent,'log'):agent.stream(['fixture'])
    def test_specific_download_reason_survives_earlier_lock(self):
        process=type('Process',(),{'stdout':['Waiting for cache lock','Failed to fetch package'],'returncode':100,'wait':lambda self:100})()
        with patch.object(agent.subprocess,'Popen',return_value=process),patch.object(agent,'log'):
            with self.assertRaises(agent.OperationError) as error:agent.stream(['fixture'])
            self.assertEqual(error.exception.code,'package_fetch_failed')
    def test_json_and_background_error_protocol(self):
        self.assertEqual(agent.error_code(json.JSONDecodeError('private','',0)),'json_invalid')
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);(directory/'error.json').write_text(json.dumps({'error':'fixture error','code':'mount_failed'}))
            with patch.object(agent,'job_directory',return_value=directory):
                report=agent.poll_job('fixture',0)
                self.assertTrue(report['done']);self.assertEqual(report['errorCode'],'mount_failed')
if __name__=='__main__':unittest.main()
