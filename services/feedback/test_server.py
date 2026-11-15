import http.client,json,tempfile,threading,unittest,uuid,subprocess,sys
from pathlib import Path
from server import Server,Store

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'db.sqlite');self.server=Server(('127.0.0.1',0),self.store);threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def post(self,path,data,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port);c.request('POST',path,json.dumps(data),{'Content-Type':'application/json',**(headers or {})});r=c.getresponse();r.read();status=r.status;c.close();return status
    def usage(self):return dict(schema=1,eventId=str(uuid.uuid4()),installationId=str(uuid.uuid4()),version='0.4.0',os='Windows',eventType='launch')
    def test_daily_counts_retry_and_no_sensitive_columns(self):
        event=self.usage();self.assertEqual(self.post('/v1/usage',event),202);self.assertEqual(self.post('/v1/usage',event),202)
        event['eventId']=str(uuid.uuid4());self.assertEqual(self.post('/v1/usage',event),202)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*),count(distinct installation_id) FROM usage').fetchone(),(2,1))
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(usage)')],['event_id','installation_id','day','version','os'])
    def test_macos_usage_and_unknown_platform(self):
        event=self.usage();event['os']='macOS';self.assertEqual(self.post('/v1/usage',event),202)
        event['eventId']=str(uuid.uuid4());event['os']='unknown';self.assertEqual(self.post('/v1/usage',event),400)
    def test_reject_unexpected_personal_data(self):
        event=self.usage();event['password']='must-not-store';self.assertEqual(self.post('/v1/usage',event),400)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM usage').fetchone()[0],0)
    def test_feedback_independent_from_telemetry_and_rate_limited(self):
        for i in range(5):self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId=str(uuid.uuid4()),version='0.4.0',message='测试反馈文字')),202)
        self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId=str(uuid.uuid4()),version='0.4.0',message='测试反馈文字')),429)
        self.assertEqual(self.post('/v1/usage',self.usage()),202)
    def test_admin_not_public_and_size_limit(self):
        self.assertEqual(self.post('/admin',{}),404)
        self.assertEqual(self.post('/v1/feedback',{'message':'x'*40000}),413)
        self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId='bad',version='0.4.0',message='测试反馈文字')),400)
    def test_full_chinese_feedback_escaped_json(self):
        self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId=str(uuid.uuid4()),version='0.4.0',message='测'*4000)),202)
    def test_beta_version(self):
        event=self.usage();event['version']='Beta-0.4.3.1';self.assertEqual(self.post('/v1/usage',event),202)
        self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId=str(uuid.uuid4()),version='Beta-0.4.3.1',message='Beta test feedback')),202)
    def diagnostic(self):
        return dict(schema=1,eventId=str(uuid.uuid4()),version='Beta-0.4.3.3',report=dict(operation='chrome',outcome='failure',errorKind='package_lock_wait',failureSite='remote_task',durationSeconds=180,signals=[dict(seconds=2,code='job_started'),dict(seconds=180,code='package_lock_wait')],omittedLines=27))
    def diagnostic_v2(self):
        event=self.diagnostic();event['schema']=2
        event['context']={key:str(uuid.uuid4()) for key in ['installationId','sessionId','operationId']}
        event['context']['operationId']=event['eventId'];event['report']['stage']='job_started'
        event['report']['errorKind']='json_invalid'
        return event
    def test_v2_correlation_feedback_and_legacy_coexist(self):
        event=self.diagnostic_v2();self.assertEqual(self.post('/v1/diagnostics',event),202)
        feedback=dict(schema=2,eventId=str(uuid.uuid4()),version=event['version'],message='最近一次操作失败',context=dict(correlation=event['context'],report=event['report']))
        self.assertEqual(self.post('/v1/feedback',feedback),202)
        self.assertEqual(self.post('/v1/diagnostics',self.diagnostic()),202)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM report_context').fetchone()[0],2)
            stored=json.loads(db.execute('SELECT context FROM report_context WHERE event_id=?',(feedback['eventId'],)).fetchone()[0])
            self.assertEqual(stored['correlation'],event['context']);self.assertEqual(stored['report']['stage'],'job_started')
        feedback['eventId']=str(uuid.uuid4());feedback['context']=None
        self.assertEqual(self.post('/v1/feedback',feedback),202)
    def test_v2_reject_private_metadata_invalid_stages_and_mismatched_ids(self):
        for mutation in ['extra','id','stage','raw','feedback_raw']:
            event=self.diagnostic_v2();path='/v1/diagnostics'
            if mutation=='extra':event['context']['hostname']='private'
            elif mutation=='id':event['context']['operationId']=str(uuid.uuid4())
            elif mutation=='stage':event['report']['stage']='private path'
            elif mutation=='raw':event['report']['message']='secret'
            else:
                event=dict(schema=2,eventId=str(uuid.uuid4()),version=event['version'],message='用户主动描述',context=dict(correlation=event['context'],report=event['report'],logs='secret'));path='/v1/feedback'
            self.assertEqual(self.post(path,event),400)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM report_context').fetchone()[0],0)
    def test_report_queries_correlated_issue_and_full_summary(self):
        event=self.diagnostic_v2();self.store.insert('diagnostics',event)
        feedback=dict(schema=2,eventId=str(uuid.uuid4()),version=event['version'],message='最近一次操作失败',context=dict(correlation=event['context'],report=event['report']))
        self.store.insert('feedback',feedback)
        def report(*args):return json.loads(subprocess.check_output([sys.executable,str(Path(__file__).with_name('report.py')),self.store.path,*args],text=True))
        self.assertEqual(report('--feedback','--issue',event['eventId'])[0]['event_id'],feedback['eventId'])
        self.assertEqual(report('--diagnostics','--issue',event['eventId'])[0]['context'],event['context'])
        self.assertEqual(report('--summary')[0]['reports'],1)
    def test_context_retention_follows_parent(self):
        event=self.diagnostic_v2();self.store.insert('diagnostics',event)
        with self.store.connect() as db:db.execute("UPDATE diagnostics SET created_at='2000-01-01' WHERE event_id=?",(event['eventId'],))
        self.store.insert('diagnostics',self.diagnostic())
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM report_context').fetchone()[0],0)
    def test_diagnostics_retry_deduplication_and_fixed_fields(self):
        event=self.diagnostic()
        self.assertEqual(self.post('/v1/diagnostics',event),202);self.assertEqual(self.post('/v1/diagnostics',event),202)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM diagnostics').fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('SELECT report FROM diagnostics').fetchone()[0]),event['report'])
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(diagnostics)')],['event_id','created_at','version','report'])
    def test_diagnostics_reject_raw_text_and_identifiers_at_every_level(self):
        events=[]
        for field in ['message','installationId','password','stackTrace','logs']:
            event=self.diagnostic();event[field]='must-not-store';events.append(event)
        for field in ['operation','outcome','errorKind','failureSite','signals','durationSeconds','omittedLines']:
            event=self.diagnostic();event['report'][field]=r'C:\Users\private password=secret https://server.invalid/token';events.append(event)
        event=self.diagnostic();event['report']['message']='secret';events.append(event)
        event=self.diagnostic();event['report']['signals'][0]['code']='secret';events.append(event)
        event=self.diagnostic();event['report']['signals'][0]['message']='secret';events.append(event)
        for event in events:self.assertEqual(self.post('/v1/diagnostics',event),400)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM diagnostics').fetchone()[0],0)
    def test_diagnostics_bounds_and_consistency(self):
        events=[]
        for field,value in [('durationSeconds',True),('durationSeconds',86401),('omittedLines',-1),('outcome','success')]:
            event=self.diagnostic();event['report'][field]=value;events.append(event)
        for signals in [[dict(seconds=181,code='job_started')],[dict(seconds=True,code='job_started')],[dict(seconds=2,code='job_started'),dict(seconds=1,code='package_lock_wait')],[dict(seconds=0,code='job_started')]*65]:
            event=self.diagnostic();event['report']['signals']=signals;events.append(event)
        for event in events:self.assertEqual(self.post('/v1/diagnostics',event),400)
    def test_diagnostics_rate_limit_is_independent(self):
        for _ in range(20):self.assertEqual(self.post('/v1/diagnostics',self.diagnostic()),202)
        self.assertEqual(self.post('/v1/diagnostics',self.diagnostic()),429)
        self.assertEqual(self.post('/v1/feedback',dict(schema=1,eventId=str(uuid.uuid4()),version='0.4.0',message='normal feedback')),202)
        self.assertEqual(self.post('/v1/usage',self.usage()),202)
    def test_diagnostics_retention_does_not_shorten_feedback_retention(self):
        import datetime as dt
        old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=31)).isoformat()
        with self.store.connect() as db:
            db.execute('INSERT INTO diagnostics VALUES(?,?,?,?)',('old',old,'0.4.0','{}'))
            db.execute('INSERT INTO feedback VALUES(?,?,?,?)',('retained',old,'0.4.0','keep-feedback'))
        self.assertEqual(self.post('/v1/diagnostics',self.diagnostic()),202)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM diagnostics WHERE event_id='old'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback WHERE event_id='retained'").fetchone()[0],1)
    def test_retention(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO usage VALUES(?,?,?,?,?)',('old','old','2000-01-01','0.1.0','Windows'));db.execute('INSERT INTO feedback VALUES(?,?,?,?)',('old','2000-01-01T00:00:00+00:00','0.1.0','old-feedback'))
        self.post('/v1/usage',self.usage())
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM feedback").fetchone()[0],0);self.assertEqual(db.execute("SELECT count(*) FROM usage WHERE event_id='old'").fetchone()[0],0)
if __name__=='__main__':unittest.main()
