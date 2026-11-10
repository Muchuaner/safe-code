#!/usr/bin/env python3
"""Safe Code 自建反馈与启动统计接口。仅监听回环，前置 HTTPS。"""
import datetime as dt
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

VERSION=re.compile(r'^(?:Beta-)?\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?$')
OPERATIONS={'account','localization','desktop','chrome','input','claude','ssh','cloud','mount_install','mount_apply','connection','ip_policy'}
SIGNALS={'package_lock_wait','dns_failure','disk_full','package_fetch_failed','package_interrupted','remote_started','packages_present','request_uploaded','job_started','ssh_authenticated','agent_ready','package_unpacking','package_configuring'}
ERRORS={'none','cancelled','directory_not_found','file_not_found','access_denied','timeout','crypto_error','http_error','io_error','invalid_argument','operation_failed'}|SIGNALS
SITES={'unknown','account_draft_clear','account_draft_save','account_draft_read','remote_task','directory_mount','connection'}
def validate_diagnostic(report):
    fields={'operation','outcome','errorKind','failureSite','durationSeconds','signals','omittedLines'}
    if not isinstance(report,dict) or set(report)!=fields:raise ValueError('诊断字段不受支持')
    for key,allowed in [('operation',OPERATIONS),('outcome',{'success','failure','cancelled'}),('errorKind',ERRORS),('failureSite',SITES)]:
        if not isinstance(report[key],str) or report[key] not in allowed:raise ValueError('诊断值不受支持')
    for key,maximum in [('durationSeconds',86400),('omittedLines',1000000)]:
        if type(report[key]) is not int or not 0<=report[key]<=maximum:raise ValueError('诊断数值无效')
    if (report['outcome']=='success')!=(report['errorKind']=='none'):raise ValueError('诊断结果不一致')
    if not isinstance(report['signals'],list) or len(report['signals'])>64:raise ValueError('诊断日志数量无效')
    previous=0
    for signal in report['signals']:
        if not isinstance(signal,dict) or set(signal)!={'seconds','code'}:raise ValueError('诊断日志字段无效')
        if not isinstance(signal['code'],str) or signal['code'] not in SIGNALS:raise ValueError('诊断日志值无效')
        if type(signal['seconds']) is not int or not previous<=signal['seconds']<=report['durationSeconds']:raise ValueError('诊断日志时间无效')
        previous=signal['seconds']

class Store:
    def __init__(self,path):
        self.path=str(path)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS usage(event_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, day TEXT NOT NULL, version TEXT NOT NULL, os TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
            CREATE TABLE IF NOT EXISTS feedback(event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, version TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS diagnostics(event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, version TEXT NOT NULL, report TEXT NOT NULL);''')
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.execute('PRAGMA busy_timeout=5000');db.execute('PRAGMA max_page_count=32768');return db
    def insert(self,kind,data):
        required={'schema','eventId','version','installationId','os','eventType'} if kind=='usage' else {'schema','eventId','version','report'} if kind=='diagnostics' else {'schema','eventId','version','message'}
        if not isinstance(data,dict) or set(data)!=required or data.get('schema')!=1:raise ValueError('字段不受支持')
        for key in ['eventId']+(['installationId'] if kind=='usage' else []):
            if not isinstance(data[key],str) or str(uuid.UUID(data[key]))!=data[key]:raise ValueError('标识格式错误')
        if not isinstance(data['version'],str) or not VERSION.fullmatch(data['version']):raise ValueError('版本格式错误')
        now=dt.datetime.now(dt.timezone.utc);day=now.date().isoformat()
        if kind=='usage':
            if data['os']!='Windows' or data['eventType']!='launch':raise ValueError('事件不受支持')
        elif kind=='diagnostics':validate_diagnostic(data['report'])
        elif not isinstance(data['message'],str) or not 5<=len(data['message'].strip())<=4000:raise ValueError('反馈长度无效')
        with self.connect() as db:
            db.execute('DELETE FROM usage WHERE day < ?',((now.date()-dt.timedelta(days=90)).isoformat(),))
            db.execute('DELETE FROM feedback WHERE created_at < ?',((now-dt.timedelta(days=180)).isoformat(),))
            db.execute('DELETE FROM diagnostics WHERE created_at < ?',((now-dt.timedelta(days=30)).isoformat(),))
            if kind=='usage':db.execute('INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?)',(data['eventId'],data['installationId'],day,data['version'],data['os']))
            elif kind=='diagnostics':db.execute('INSERT OR IGNORE INTO diagnostics VALUES(?,?,?,?)',(data['eventId'],now.isoformat(),data['version'],json.dumps(data['report'],ensure_ascii=False,separators=(',',':'))))
            else:db.execute('INSERT OR IGNORE INTO feedback VALUES(?,?,?,?)',(data['eventId'],now.isoformat(),data['version'],data['message'].strip()))

class RateLimit:
    def __init__(self):self.salt=secrets.token_bytes(32);self.items={};self.lock=threading.Lock()
    def allow(self,address,kind):
        now=time.monotonic();window=3600 if kind in ['feedback','diagnostics'] else 60;limit=5 if kind=='feedback' else 20 if kind=='diagnostics' else 120
        key=(kind,hmac.new(self.salt,address.encode(),hashlib.sha256).digest())
        with self.lock:
            self.items={k:v for k,v in self.items.items() if now-v[0]<3600}
            since,count=self.items.get(key,(now,0))
            if now-since>=window:since,count=now,0
            if count>=limit or (len(self.items)>=5000 and key not in self.items):return False
            self.items[key]=(since,count+1);return True

class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store):self.store=store;self.limiter=RateLimit();self.slots=threading.BoundedSemaphore(16);super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.slots.acquire(False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,request,address):pass # 不输出来源地址、请求正文或用户反馈。

class Handler(http.server.BaseHTTPRequestHandler):
    server_version='SafeCodeFeedback';sys_version=''
    def setup(self):super().setup();self.connection.settimeout(6)
    def log_message(self,*args):pass
    def answer(self,status,body):
        encoded=json.dumps(body,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(encoded)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(encoded)
    def do_GET(self):self.answer(200,{'ok':True}) if self.path=='/health' else self.answer(404,{'error':'not_found'})
    def do_POST(self):
        if self.path not in ['/v1/usage','/v1/feedback','/v1/diagnostics']:self.answer(404,{'error':'not_found'});return
        kind=self.path.rsplit('/',1)[1]
        # 仅当来自回环反向代理时读取由代理覆盖的专用头，来源只在内存中限流。
        address=self.headers.get('X-SafeCode-Client-IP',self.client_address[0]) if self.client_address[0] in ['127.0.0.1','::1'] else self.client_address[0]
        address=address or self.client_address[0]
        if len(address)>128 or not self.server.limiter.allow(address,kind):self.answer(429,{'error':'rate_limited'});return
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):self.answer(400,{'error':'unsupported_encoding'});return
        if self.headers.get_content_type()!='application/json':self.answer(415,{'error':'json_required'});return
        try:length=int(self.headers.get('Content-Length','-1'))
        except ValueError:length=-1
        if length<1 or length>32768:self.answer(413,{'error':'body_size'});return
        try:
            body=self.rfile.read(length)
            if len(body)!=length:raise ValueError('incomplete')
            self.server.store.insert(kind,json.loads(body))
        except (ValueError,TypeError,KeyError,UnicodeDecodeError,AttributeError):self.answer(400,{'error':'invalid_request'});return
        except sqlite3.Error:self.answer(503,{'error':'temporarily_unavailable'});return
        self.answer(202,{'accepted':True})

def main():
    os.umask(0o077)
    path=Path(os.environ.get('SAFECODE_DB','/var/lib/safe-code-feedback/data.sqlite3'));path.parent.mkdir(parents=True,exist_ok=True)
    port=int(os.environ.get('SAFECODE_PORT','18761'))
    Server(('127.0.0.1',port),Store(path)).serve_forever()
if __name__=='__main__':main()
