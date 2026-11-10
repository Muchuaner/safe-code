#!/usr/bin/env python3
"""Exercise the real unprivileged directory helper in a temporary home."""
import importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

root=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('agent',root/'src/SafeCode.Core/Remote/agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
class Targets(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve();self.home=self.base/'home';self.home.mkdir();self.destination=self.base/'managed';self.destination.mkdir()
    def helper(self,operation,relative,**kwargs):
        request={'operation':operation,'relative':relative,**kwargs}
        p=subprocess.run([sys.executable,'-I','-c',agent.USER_FILES],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(self.home)})
        if p.returncode:raise RuntimeError(p.stderr.strip().splitlines()[-1])
        return p
    def test_preflight_does_not_create_or_remove_directories(self):
        (self.home/'empty').mkdir()
        for name in ['empty','missing/subdir']:self.helper('check_alias',name,destination=str(self.destination))
        self.assertTrue((self.home/'empty').is_dir());self.assertFalse((self.home/'missing').exists())
    def test_nonempty_is_preserved(self):
        target=self.home/'project';target.mkdir();(target/'source.txt').write_text('keep')
        with self.assertRaisesRegex(RuntimeError,'服务器目录不是空目录'):self.helper('check_alias','project',destination=str(self.destination))
        self.assertEqual((target/'source.txt').read_text(),'keep')
    def test_managed_alias_can_be_validated_and_removed(self):
        self.helper('alias','shared',destination=str(self.destination));self.helper('check_alias','shared',destination=str(self.destination))
        self.helper('remove_alias','shared',destination=str(self.destination));self.assertTrue((self.home/'shared').is_dir());self.assertFalse((self.home/'shared').is_symlink())
    def test_foreign_symlink_is_preserved(self):
        (self.home/'shared').symlink_to(self.destination)
        with self.assertRaisesRegex(RuntimeError,'其他符号链接'):self.helper('check_alias','shared',destination=str(self.base/'other'))
        self.assertTrue((self.home/'shared').is_symlink())
    def test_parent_symlink_outside_home_is_rejected(self):
        (self.home/'outside').symlink_to(self.destination)
        with self.assertRaisesRegex(RuntimeError,'主目录之外'):self.helper('check_alias','outside/child',destination=str(self.destination))
    def test_invalid_target_does_not_remove_existing_mounts(self):
        target=self.home/'project';target.mkdir();(target/'source').write_text('keep')
        with patch.object(agent,'account',return_value={'username':'fixture'}),patch.object(agent,'home',return_value=self.home),patch.object(agent,'package_installed',return_value=True),patch.object(agent,'log'),patch.object(agent,'STATE',self.base),patch.object(agent,'user_files',side_effect=self.helper),patch.object(agent,'remove_mounts') as remove,patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'127.0.0.1:12345','')):
            with self.assertRaisesRegex(RuntimeError,'服务器目录不是空目录'):agent.apply_mounts({'clientId':'a'*20,'port':12345,'username':'safecode','password':'b'*48,'mappings':[{'remote':str(target)}]})
            remove.assert_not_called()
if __name__=='__main__':unittest.main()
