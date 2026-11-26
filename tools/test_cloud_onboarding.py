"""驗證僅跳過技術引導，保留用戶自行註冊的網頁帳號密碼。"""
import importlib.util,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class CloudOnboarding(unittest.TestCase):
    def initialize(self,home,name='workspace',email='workspace@safe-code.invalid'):
        request={'operation':'cloud_onboarding','relative':'.local/state/safe-code-cloud/auth.db','gitName':name,'gitEmail':email}
        return subprocess.run([sys.executable,'-I','-c',a.USER_FILES],input=json.dumps(request),capture_output=True,text=True,env={**os.environ,'HOME':str(home)})
    def fixture(self,home):
        db=home/'.local/state/safe-code-cloud/auth.db';db.parent.mkdir(parents=True)
        c=sqlite3.connect(db);c.execute('CREATE TABLE users (id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,is_active INTEGER DEFAULT 1,git_name TEXT,git_email TEXT,has_completed_onboarding INTEGER DEFAULT 0)')
        c.execute("INSERT INTO users(username,password_hash,git_name,git_email) VALUES ('owner','original-hash','Existing Name','existing@example.invalid')");c.commit();return c
    def test_existing_account_and_future_self_registration(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);c=self.fixture(home);r=self.initialize(home,"O'Reilly ; DROP TABLE users;");self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(c.execute('SELECT username,password_hash,git_name,git_email,has_completed_onboarding FROM users').fetchone(),('owner','original-hash','Existing Name','existing@example.invalid',1))
            c.execute("INSERT INTO users(username,password_hash) VALUES ('self-chosen-account','self-chosen-password-hash')");c.commit()
            row=c.execute("SELECT username,password_hash,git_name,git_email,has_completed_onboarding FROM users WHERE username='self-chosen-account'").fetchone()
            self.assertEqual(row,('self-chosen-account','self-chosen-password-hash',"O'Reilly ; DROP TABLE users;",'workspace@safe-code.invalid',1));c.close()
    def test_idempotency_preserves_original_initialization_backup(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);c=self.fixture(home)
            self.assertEqual(self.initialize(home).returncode,0);backup=home/'.local/state/safe-code-cloud/onboarding-before-safe-code.json';first=backup.read_bytes()
            self.assertEqual(self.initialize(home).returncode,0);self.assertEqual(backup.read_bytes(),first)
            self.assertNotIn(b'original-hash',first);self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='safe_code_cloudcli_onboarding'").fetchone()[0],1);c.close()
    def test_unknown_schema_is_not_modified(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);db=home/'.local/state/safe-code-cloud/auth.db';db.parent.mkdir(parents=True);c=sqlite3.connect(db);c.execute('CREATE TABLE users(id INTEGER,username TEXT)');c.execute("INSERT INTO users VALUES(1,'unchanged')");c.commit()
            self.assertNotEqual(self.initialize(home).returncode,0);self.assertEqual(c.execute('SELECT * FROM users').fetchall(),[(1,'unchanged')]);c.close()
    def test_existing_global_git_identity_is_not_overwritten(self):
        with patch.object(a,'account',return_value={'username':'workspace'}),patch.object(a.pwd,'getpwnam',return_value=Mock(pw_uid=1000,pw_dir='/home/workspace')),patch.object(a.shutil,'which',return_value='/usr/bin/git'),patch.object(a,'run',side_effect=[subprocess.CompletedProcess([],0,'Existing Name\n'),subprocess.CompletedProcess([],0,'existing@example.invalid\n')]) as run:
            self.assertEqual(a.setup_git_identity(),{'user.name':'Existing Name','user.email':'existing@example.invalid'});self.assertEqual(run.call_count,2)
if __name__=='__main__':unittest.main()
