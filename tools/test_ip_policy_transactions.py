#!/usr/bin/env python3
"""验证过期事务清理、独立应用和持久化失败回退，不改宿主网络。"""
import importlib.util,json,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

root=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('network',root/'src/SafeCode.Core/Remote/network.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)
class Transactions(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
        self.state=self.base/'state';self.state.mkdir();self.opt=self.base/'opt/safe-code';self.opt.mkdir(parents=True)
        self.unit=self.base/'etc/systemd/system/safe-code-ip-policy.service';self.unit.parent.mkdir(parents=True)
        nft=self.base/'usr/sbin/nft';nft.parent.mkdir(parents=True);nft.touch()
        self.route={'tokens':['default','via','198.18.0.1','dev','ens18','src','198.18.0.12'],'dev':'ens18','source':'198.18.0.12'}
        self.policy={'ingressIp':'198.18.0.11','egressIp':'198.18.0.12','sshPort':22,'lockIngress':True,'lockEgress':True}
        self.commands=[];self.fail_enable=False
        def local_path(value):
            text=str(value)
            return self.base/text.lstrip('/') if text.startswith(('/opt/','/etc/','/usr/')) else Path(value)
        def run(args,check=True,stdin=None):
            self.commands.append((args,stdin))
            if args==['systemctl','enable',n.SERVICE] and self.fail_enable:
                self.fail_enable=False;raise RuntimeError('fixture enable failure')
            return subprocess.CompletedProcess(args,0,'enabled\n' if args[:2]==['systemctl','is-enabled'] else '198.18.0.12' if args[0]=='curl' else '', '')
        for name,value in [('ROOT',self.state),('Path',local_path),('run',run),('route',lambda:self.route),('valid_policy',lambda p:p),('existing_table',lambda:'table inet safe_code_ip { comment "Safe Code managed"; }')]:
            handle=patch.object(n,name,value);handle.start();self.addCleanup(handle.stop)
    def test_stale_unapplied_record_is_cleared_without_touching_network(self):
        old=self.state/('ip-pending-'+'a'*24+'.json');old.write_text(json.dumps({'created':time.time()-601,'applied':False}))
        result=n.prepare(self.policy)
        self.assertFalse(old.exists());self.assertTrue((self.state/('ip-pending-'+result['token']+'.json')).exists())
        self.assertFalse(any(args[0] in ['ip','nft'] for args,_ in self.commands))
    def test_applied_and_recent_records_are_not_discarded(self):
        for applied,age in [(False,20),(True,2000)]:
            path=self.state/('ip-pending-'+'b'*24+'.json');path.write_text(json.dumps({'created':time.time()-age,'applied':applied}))
            with self.assertRaisesRegex(RuntimeError,'已有网络变更'):n.prepare(self.policy)
            self.assertTrue(path.exists());path.unlink()
    def test_scheduled_application_is_independent_and_waits_until_ready(self):
        token=n.prepare(self.policy)['token'];response=n.schedule_apply(token)
        self.assertTrue(response['scheduled']);self.assertFalse(n.status(token)['ready'])
        with patch.object(n,'apply_rules'),patch.object(n,'replace_source'):n.apply(token)
        self.assertTrue(n.status(token)['ready'])
        self.assertTrue(any(args[0]=='systemd-run' and '--on-active=120s' in args for args,_ in self.commands))
    def test_persistence_failure_restores_previous_boot_configuration(self):
        old_policy={**self.policy,'lockIngress':False};(self.state/'ip-policy.json').write_text(json.dumps(old_policy))
        self.unit.write_text('Safe Code assigned IP policy\nold-unit');(self.opt/'network.py').write_text('old-module')
        token=n.prepare(self.policy)['token']
        with patch.object(n,'apply_rules'),patch.object(n,'replace_source'):n.apply(token)
        self.fail_enable=True
        with self.assertRaisesRegex(RuntimeError,'fixture enable failure'):n.commit(token)
        self.assertEqual(json.loads((self.state/'ip-policy.json').read_text()),old_policy)
        self.assertEqual(self.unit.read_text(),'Safe Code assigned IP policy\nold-unit')
        self.assertEqual((self.opt/'network.py').read_text(),'old-module')
        self.assertFalse((self.state/('ip-pending-'+token+'.json')).exists())
        self.assertTrue(any(args[:4]==['ip','-4','route','replace'] and args[4:]==self.route['tokens'] for args,_ in self.commands))
if __name__=='__main__':unittest.main()
