#!/usr/bin/env python3
"""Exercise the real unprivileged directory helper in a temporary home."""
import importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

root=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('agent',root/'src/SafeCode.Core/Remote/agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
class Targets(unittest.TestCase):
    def test_mount_target_code_reaches_remote_protocol(self):
        self.assertEqual(agent.error_code(agent.OperationError('mount_target_invalid','fixture')),'mount_target_invalid')
        self.assertEqual(agent.error_code(agent.OperationError('mount_failed','fixture')),'mount_failed')
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
            with self.assertRaises(agent.OperationError) as raised:agent.apply_mounts({'clientId':'a'*20,'port':12345,'username':'safecode','password':'b'*48,'mappings':[{'remote':str(target)}]})
            self.assertEqual(raised.exception.code,'mount_target_invalid')
            remove.assert_not_called()
    def test_create_empty_folder_and_reject_existing_without_touching_files(self):
        value=json.loads(self.helper('mkdir','Project-2026 中文').stdout);self.assertEqual(value['path'],str(self.home/'Project-2026 中文'));self.assertEqual(list(Path(value['path']).iterdir()),[])
        with self.assertRaisesRegex(RuntimeError,'同名'):self.helper('mkdir','Project-2026 中文')
    def test_create_folder_rejects_escape_and_path_as_name(self):
        with patch.object(agent,'home',return_value=self.home),patch.object(agent,'user_files',side_effect=self.helper):
            for name in ['../outside','a/b','..','a\\b']:
                with self.assertRaises(RuntimeError):agent.create_directory({'path':str(self.home),'name':name})
            value=agent.create_directory({'path':str(self.home),'name':'new-empty'});self.assertTrue(Path(value['path']).is_dir())
            (self.home/'link').symlink_to(self.destination)
            with self.assertRaises(RuntimeError):agent.create_directory({'path':str(self.home/'link'),'name':'escape'})
    def test_mount_cleanup_failure_does_not_hide_mount_failure(self):
        target=self.home/'project';target.mkdir()
        responses=[{'clientId':'a'*20,'paths':[]},RuntimeError('cleanup busy')]
        def remove(_options):
            value=responses.pop(0)
            if isinstance(value,Exception):raise value
            return value
        mounts=[];secrets=self.base/'secrets'
        def mount(args,**kwargs):
            mounts.append(args);return subprocess.CompletedProcess(args,1,'','mount failed: fixture')
        with patch.object(agent,'account',return_value={'username':'fixture'}),patch.object(agent,'home',return_value=self.home),patch.object(agent,'package_installed',return_value=True),patch.object(agent,'log'),patch.object(agent,'STATE',self.base),patch.object(agent,'SECRETS_PATH',secrets),patch.object(agent,'user_files',return_value=subprocess.CompletedProcess([],0,'','')),patch.object(agent,'remove_mounts',side_effect=remove),patch.object(agent,'mounted_paths',return_value=set()),patch.object(agent,'secure_root_directory'),patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'127.0.0.1:12345','')),patch.object(agent.subprocess,'run',side_effect=mount),patch.object(agent.pwd,'getpwnam',return_value=type('Entry',(),{'pw_uid':1000,'pw_gid':1000})()):
            with self.assertRaises(agent.OperationError) as raised:agent.apply_mounts({'clientId':'a'*20,'port':12345,'username':'safecode','password':'b'*48,'mappings':[{'remote':str(target)}]})
            self.assertEqual(raised.exception.code,'mount_failed')
            self.assertIn('mount failed: fixture',str(raised.exception))
            self.assertIn('cleanup busy',str(raised.exception))
            self.assertEqual(len(mounts),1)

    def test_failed_new_mount_restores_previous_record_and_credentials(self):
        client='a'*20;target=self.home/'new-project';target.mkdir()
        old_target=self.base/'mnt/old/m0';old_target.mkdir(parents=True)
        alias=self.home/'shared';alias.mkdir()
        state=self.base/'state';state.mkdir(exist_ok=True)
        record=state/('mount-'+client+'.json')
        record.write_text(json.dumps({'paths':[str(old_target)],'urls':['http://127.0.0.1:41000/m0/'],'aliases':[str(alias)]}))
        config=state/('mount-'+client)/'davfs2.conf';config.parent.mkdir(parents=True,exist_ok=True);config.write_text('fixture')
        secrets=self.base/'davfs2.secrets'
        secrets.write_text('# safe-code-'+client+'\nhttp://127.0.0.1:41000/m0/ safecode '+'c'*48+'\n')
        calls=[]
        def remove(_options):
            if record.exists():record.unlink()
            if secrets.exists():
                lines=[line for line in secrets.read_text().splitlines() if line!='# safe-code-'+client and '41000' not in line]
                secrets.write_text('\n'.join(lines)+'\n')
            return {'clientId':client,'paths':[]}
        def mount(target,url,config,uid,gid):
            calls.append((str(target),url))
            if '42000' in url:raise agent.OperationError('mount_failed','new mount failed')
        with patch.object(agent,'account',return_value={'username':'fixture'}),patch.object(agent,'home',return_value=self.home),patch.object(agent,'package_installed',return_value=True),patch.object(agent,'log'),patch.object(agent,'STATE',state),patch.object(agent,'SECRETS_PATH',secrets),patch.object(agent,'user_files',return_value=subprocess.CompletedProcess([],0,'','')),patch.object(agent,'remove_mounts',side_effect=remove),patch.object(agent,'mounted_paths',return_value=set()),patch.object(agent,'secure_root_directory'),patch.object(agent,'mount_davfs',side_effect=mount),patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'127.0.0.1:42000','')),patch.object(agent.pwd,'getpwnam',return_value=type('Entry',(),{'pw_uid':1000,'pw_gid':1000})()):
            with self.assertRaises(agent.OperationError) as raised:agent.apply_mounts({'clientId':client,'port':42000,'username':'safecode','password':'d'*48,'mappings':[{'remote':str(target)}]})
            self.assertEqual(raised.exception.code,'mount_failed')
        restored=json.loads(record.read_text())
        self.assertEqual(restored['aliases'],[str(alias)])
        self.assertEqual(restored['paths'],[str(old_target)])
        self.assertIn('41000',secrets.read_text())
        self.assertIn((str(old_target),'http://127.0.0.1:41000/m0/'),calls)

if __name__=='__main__':unittest.main()
