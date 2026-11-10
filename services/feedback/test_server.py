import http.client,json,tempfile,threading,unittest,uuid
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
