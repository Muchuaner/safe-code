"""Exercise the actual unprivileged project transaction; no VPS or model calls."""
import importlib.util,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('agent',Path(__file__).resolve().parents[1]/'src/SafeCode.Core/Remote/agent.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class CloudProjects(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.home=Path(self.temp.name).resolve()
        self.db=self.home/'.local/state/safe-code-cloud/auth.db';self.db.parent.mkdir(parents=True)
        self.c=sqlite3.connect(self.db);self.addCleanup(self.c.close)
        self.c.executescript('CREATE TABLE projects(project_id TEXT PRIMARY KEY NOT NULL,project_path TEXT NOT NULL UNIQUE,custom_project_name TEXT DEFAULT NULL,isStarred BOOLEAN DEFAULT 0,isArchived BOOLEAN DEFAULT 0); CREATE TABLE sessions(id TEXT,project_path TEXT,content TEXT); CREATE TABLE users(username TEXT,password_hash TEXT); INSERT INTO users VALUES("owner","preserved");')
        self.c.commit()
    def sync(self,projects,success=True):
        request={'operation':'cloud_projects','relative':'.local/state/safe-code-cloud/auth.db','projects':projects}
        p=subprocess.run([sys.executable,'-I','-c',a.USER_FILES],input=json.dumps(request),text=True,capture_output=True,env={**os.environ,'HOME':str(self.home)})
        if success:self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
        self.assertNotEqual(p.returncode,0);return p
    def test_multiple_directories_unicode_and_repeat_keep_ids(self):
        projects=[{'path':'/mnt/safe-code/workspace/c/p1','name':'項目 中文'},{'path':'/mnt/safe-code/workspace/c/p2','name':'second'}]
        self.assertTrue(self.sync(projects)['changed']);first=self.c.execute('SELECT * FROM projects ORDER BY project_path').fetchall()
        self.assertFalse(self.sync(projects[::-1])['changed']);self.assertEqual(first,self.c.execute('SELECT * FROM projects ORDER BY project_path').fetchall());self.assertEqual(first[0][2],'項目 中文')
    def test_unmount_archives_history_and_remount_restores_same_id(self):
        project={'path':'/mnt/safe-code/workspace/c/p1','name':'one'};self.sync([project]);identifier=self.c.execute('SELECT project_id FROM projects').fetchone()[0]
        self.c.execute('INSERT INTO sessions VALUES(?,?,?)',('s1',project['path'],'keep history'));self.c.execute('UPDATE projects SET custom_project_name="My title",isStarred=1');self.c.commit()
        self.sync([]);self.assertEqual(self.c.execute('SELECT isArchived FROM projects').fetchone()[0],1)
        self.sync([project]);self.assertEqual(self.c.execute('SELECT project_id,custom_project_name,isStarred,isArchived FROM projects').fetchone(),(identifier,'My title',1,0));self.assertEqual(self.c.execute('SELECT content FROM sessions').fetchone()[0],'keep history')
    def test_manually_added_archived_project_and_credentials_are_preserved(self):
        self.c.execute('INSERT INTO projects VALUES("manual","/mnt/safe-code/workspace/c/p1","custom",1,1)');self.c.commit()
        self.sync([{'path':'/mnt/safe-code/workspace/c/p1','name':'not used'}]);self.sync([])
        self.assertEqual(self.c.execute('SELECT * FROM projects').fetchone(),('manual','/mnt/safe-code/workspace/c/p1','custom',1,1));self.assertEqual(self.c.execute('SELECT * FROM users').fetchone(),('owner','preserved'))
    def test_failed_transaction_rolls_back_all_insertions(self):
        self.c.execute("CREATE TRIGGER fail_project BEFORE INSERT ON projects WHEN NEW.project_path='bad' BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.c.commit()
        self.sync([{'path':'good','name':'one'},{'path':'bad','name':'two'}],False);self.assertEqual(self.c.execute('SELECT count(*) FROM projects').fetchone()[0],0)
    def test_unknown_schema_never_modifies_projects(self):
        self.c.execute('DROP TABLE projects');self.c.execute('CREATE TABLE projects(unknown TEXT)');self.c.execute('INSERT INTO projects VALUES("keep")');self.c.commit()
        self.sync([],False);self.assertEqual(self.c.execute('SELECT * FROM projects').fetchall(),[('keep',)])
    def test_legacy_paths_survive_unmount_and_row_reordering(self):
        client='a'*20;one=str(self.home/'one');two=str(self.home/'two');legacy='/mnt/safe-code/workspace/'+client+'/m0'
        with patch.object(a,'STATE',self.home),patch.object(a,'account',return_value={'username':'workspace'}):
            paths,registry,saved=a.mount_project_paths(client,[one,two],{one:legacy});a.write(registry,json.dumps(saved),0o600)
            again,_,_=a.mount_project_paths(client,[two,one],{})
            self.assertEqual(again,list(reversed(paths)));self.assertEqual(again[1],legacy)
    def test_invalid_saved_paths_fail_before_mount_changes(self):
        client='a'*20;alias=str(self.home/'one');(self.home/('mount-project-paths-'+client+'.json')).write_text(json.dumps({alias:'/etc/unsafe'}))
        with patch.object(a,'STATE',self.home),patch.object(a,'account',return_value={'username':'workspace'}):
            with self.assertRaisesRegex(RuntimeError,'保留現有掛載'):a.mount_project_paths(client,[alias],{})
    def test_database_absent_is_not_created(self):
        self.c.close();self.db.unlink();self.assertEqual(self.sync([]),{'ready':False,'changed':False});self.assertFalse(self.db.exists())
    def test_only_mounted_paths_for_current_user_are_registered_without_stat(self):
        state=self.home/'state';state.mkdir();client='a'*20
        paths=['/mnt/safe-code/workspace/'+client+'/p1','/mnt/safe-code/other/'+client+'/p2','/mnt/safe-code/workspace/'+client+'/p3']
        (state/('mount-'+client+'.json')).write_text(json.dumps({'paths':paths,'aliases':[str(self.home/'中文'),str(self.home/'other'),str(self.home/'offline')]}))
        real_read=Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/proc/self/mountinfo':return '\n'.join('1 2 0:1 / '+p+' rw - fuse.davfs davfs rw' for p in paths[:2])
            return real_read(path,*args,**kwargs)
        real_stat=Path.stat
        def stat(path,*args,**kwargs):
            if str(path).startswith('/mnt/'):raise AssertionError('must not access FUSE')
            return real_stat(path,*args,**kwargs)
        with patch.object(a,'STATE',state),patch.object(a,'home',return_value=self.home),patch.object(a,'account',return_value={'username':'workspace'}),patch.object(Path,'read_text',read),patch.object(Path,'stat',stat):
            self.assertEqual(a.cloud_project_targets(),[{'path':paths[0],'name':'中文'}])
if __name__=='__main__':unittest.main()
