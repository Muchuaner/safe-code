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
    def test_retention(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO usage VALUES(?,?,?,?,?)',('old','old','2000-01-01','0.1.0','Windows'));db.execute('INSERT INTO feedback VALUES(?,?,?,?)',('old','2000-01-01T00:00:00+00:00','0.1.0','old-feedback'))
        self.post('/v1/usage',self.usage())
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM feedback").fetchone()[0],0);self.assertEqual(db.execute("SELECT count(*) FROM usage WHERE event_id='old'").fetchone()[0],0)
if __name__=='__main__':unittest.main()
