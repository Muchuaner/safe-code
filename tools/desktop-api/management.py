"""Local-only Safe Code API administration and request scheduling."""
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
import json
from pathlib import Path
import re
import select
import socket
import sqlite3
import ssl
import threading
import time

from desktop_gateway import GatewayError
from pricing import TOKEN_FIELDS, estimate


KEY_ID = re.compile(r'[a-f0-9]{64}')
LOG_RETENTION_SECONDS = 30 * 86400
ANALYTICS_RANGES = {'1h': (300, 12), '24h': (3600, 24), '7d': (86400, 7)}
LATENCY_LIMITS = (250, 500, 1000, 2500, 5000, 10000)


class KeyRegistry:
    def __init__(self, root):
        self.root = root
        self.path = root / 'gateway.sqlite3'
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS keys (
                    id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', rpm INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS usage (
                    key_id TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL,
                    failures INTEGER NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,
                    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, last_used INTEGER NOT NULL,
                    PRIMARY KEY (key_id, model)
                );
                CREATE TABLE IF NOT EXISTS request_log (
                    id INTEGER PRIMARY KEY, key_id TEXT NOT NULL, model TEXT NOT NULL,
                    at INTEGER NOT NULL, outcome TEXT NOT NULL, status INTEGER,
                    wait_ms INTEGER NOT NULL, duration_ms INTEGER, tokens INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_log_time ON request_log(at, id);
                CREATE INDEX IF NOT EXISTS request_log_key ON request_log(key_id, at, id);
            """)
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(keys)')}
            for column, kind in (('budget_nanos', 'INTEGER'), ('expires_at', 'INTEGER'), ('allowed_models', 'TEXT'),
                                 ('unrestricted', 'INTEGER NOT NULL DEFAULT 0')):
                if column not in columns:
                    db.execute(f'ALTER TABLE keys ADD COLUMN {column} {kind}')
            if 'cost_nanos' not in {row['name'] for row in db.execute('PRAGMA table_info(usage)')}:
                db.execute('ALTER TABLE usage ADD COLUMN cost_nanos INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE usage ADD COLUMN unpriced_tokens INTEGER NOT NULL DEFAULT 0')
                for key, model, *tokens in db.execute(
                        'SELECT key_id,model,input,output,cache_read,cache_write FROM usage').fetchall():
                    cost, unpriced = estimate(model, tokens, historical=True)
                    db.execute('UPDATE usage SET cost_nanos=?,unpriced_tokens=? WHERE key_id=? AND model=?',
                               (cost, unpriced, key, model))
            db.executemany('INSERT OR IGNORE INTO keys(id,created) VALUES(?,?)',
                           [(p.name, int(p.stat().st_mtime)) for p in (root/'keys').iterdir()
                            if KEY_ID.fullmatch(p.name) and p.is_file()])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def has_marker(self, key_id):
        return (self.root/'keys'/key_id).is_file()

    def active_policy(self, key_id):
        if not self.has_marker(key_id):
            return None
        with self.connect() as db:
            return db.execute('SELECT allowed_models, unrestricted FROM keys WHERE id=? AND revoked=0 '
                              'AND (expires_at IS NULL OR expires_at>?)', (key_id, time.time())).fetchone()

    def enabled(self, key_id):
        return self.active_policy(key_id) is not None

    def unrestricted(self, key_id):
        policy = self.active_policy(key_id)
        return bool(policy and policy['unrestricted'])

    def model_policy(self, key_id):
        policy = self.active_policy(key_id)
        if policy is None:
            raise GatewayError('API Key has expired or been revoked', 401)
        return None if policy['allowed_models'] is None else frozenset(json.loads(policy['allowed_models']))

    def register(self, key_id, note='', rpm=0, budget_nanos=None):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO keys(id,note,rpm,created,budget_nanos) VALUES(?,?,?,?,?)',
                       (key_id, note, rpm, int(time.time()), budget_nanos))

    def issue(self, key_id, note='', rpm=0, budget_nanos=None, *, activated=False,
              expires_at=None, allowed_models=None, unrestricted=False):
        marker = self.root/'keys'/key_id
        created = False
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                ids = db.execute('SELECT id FROM keys').fetchall()
                if not activated and any(self.has_marker(row['id']) for row in ids):
                    raise ValueError('添加更多 API Key 需要正确的激活码。')
                if len(ids) >= 500:
                    raise ValueError('Key 记录已达到 500 条上限。')
                db.execute('INSERT INTO keys(id,note,rpm,created,budget_nanos,expires_at,allowed_models,unrestricted) '
                           'VALUES(?,?,?,?,?,?,?,?)',
                           (key_id, note, rpm, int(time.time()), budget_nanos, expires_at, allowed_models, unrestricted))
                marker.touch(mode=0o600, exist_ok=False)
                created = True
        except BaseException:
            if created:
                marker.unlink(missing_ok=True)
            raise

    def settings(self, key_id):
        with self.connect() as db:
            row = db.execute('SELECT rpm FROM keys WHERE id=?', (key_id,)).fetchone()
        return row['rpm'] if row else 0

    def balance_exhausted(self, key_id):
        with self.connect() as db:
            return db.execute('''SELECT 1 FROM keys WHERE id=? AND budget_nanos IS NOT NULL
                AND budget_nanos <= (SELECT COALESCE(SUM(cost_nanos),0) FROM usage WHERE key_id=?)''',
                (key_id, key_id)).fetchone() is not None

    def balance(self, key_id):
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT budget_nanos FROM keys WHERE id=?', (key_id,)).fetchone()
            if row is None:
                return None
            models = [dict(model) for model in db.execute(
                'SELECT model,input,output,cache_read,cache_write,cost_nanos,unpriced_tokens FROM usage '
                'WHERE key_id=? ORDER BY requests DESC,model', (key_id,))]
        budget = row['budget_nanos']
        used = sum(model['cost_nanos'] for model in models)
        return {'budget_nanos': budget, 'used_nanos': used,
                'remaining_nanos': None if budget is None else max(0, budget-used), 'models': models}

    def update(self, key_id, note, rpm, *, budget_nanos=..., expires_at=..., allowed_models=..., unrestricted=...):
        with self.connect() as db:
            if not db.execute('UPDATE keys SET note=?, rpm=? WHERE id=?', (note,rpm,key_id)).rowcount:
                raise ValueError('找不到该 Key。')
            for column, value in (('budget_nanos', budget_nanos), ('expires_at', expires_at), ('allowed_models', allowed_models), ('unrestricted', unrestricted)):
                if value is not ...:
                    db.execute(f'UPDATE keys SET {column}=? WHERE id=?', (value, key_id))

    def revoke(self, key_id):
        with self.connect() as db:
            if not db.execute('UPDATE keys SET revoked=1 WHERE id=?', (key_id,)).rowcount:
                raise ValueError('找不到该 Key。')
            (self.root/'keys'/key_id).unlink(missing_ok=True)

    def delete(self, key_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.has_marker(key_id):
                raise ValueError('请先撤销密钥，再彻底删除。')
            db.execute('DELETE FROM request_log WHERE key_id=?', (key_id,))
            db.execute('DELETE FROM usage WHERE key_id=?', (key_id,))
            db.execute('DELETE FROM keys WHERE id=?', (key_id,))

    def record(self, key_id, model, failed, tokens, *, request=None):
        counts = [tokens.get(k, 0) for k in TOKEN_FIELDS]
        cost, unpriced = estimate(model, [max(0, count) for count in counts],
                                  one_hour_writes=max(0, tokens.get('one_hour_cache_writes', 0)))
        now = int(time.time())
        with self.connect() as db:
            db.execute("""
                INSERT INTO usage(key_id,model,requests,failures,input,output,cache_read,cache_write,last_used,cost_nanos,unpriced_tokens)
                SELECT ?,?,1,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM keys WHERE id=?)
                ON CONFLICT(key_id,model) DO UPDATE SET
                    requests=requests+1,failures=failures+excluded.failures,
                    input=input+excluded.input,output=output+excluded.output,
                    cache_read=cache_read+excluded.cache_read,cache_write=cache_write+excluded.cache_write,
                    last_used=MAX(last_used,excluded.last_used),
                    cost_nanos=cost_nanos+excluded.cost_nanos,
                    unpriced_tokens=unpriced_tokens+excluded.unpriced_tokens
            """, (key_id, model, int(failed), *counts, now, cost, unpriced, key_id))
            if request is not None:
                self._insert_request(db, key_id, now, request, sum(tokens.get(k, 0) for k in TOKEN_FIELDS))

    @staticmethod
    def _insert_request(db, key_id, now, request, tokens):
        db.execute('''
            INSERT INTO request_log(key_id,model,at,outcome,status,wait_ms,duration_ms,tokens)
            SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM keys WHERE id=?)
        ''', (key_id, request['model'], now, request['outcome'], request['status'],
              request['wait_ms'], request['duration_ms'], tokens, key_id))

    def record_unadmitted(self, key_id, request):
        with self.connect() as db:
            self._insert_request(db, key_id, int(time.time()), request, 0)

    def prune_requests(self):
        with self.connect() as db:
            db.execute('DELETE FROM request_log WHERE at < ?', (int(time.time()) - LOG_RETENTION_SECONDS,))

    def list(self):
        with self.connect() as db:
            rows = db.execute('''
                SELECT k.id,k.note,k.rpm,k.created,k.budget_nanos,k.expires_at,k.allowed_models,k.revoked,k.unrestricted,
                       COALESCE(SUM(u.cost_nanos),0) AS cost_nanos,
                       COALESCE(SUM(u.unpriced_tokens),0) AS unpriced_tokens,
                       COALESCE(SUM(u.requests),0) AS requests,COALESCE(SUM(u.failures),0) AS failures,
                       COALESCE(SUM(u.input+u.output+u.cache_read+u.cache_write),0) AS tokens,
                       MAX(u.last_used) AS last_used
                FROM keys k LEFT JOIN usage u ON u.key_id=k.id GROUP BY k.id
                ORDER BY k.created DESC,k.id
            ''').fetchall()
        now = time.time()
        keys = []
        for row in rows:
            key = dict(row)
            enabled = not key.pop('revoked') and self.has_marker(key['id'])
            key['enabled'] = enabled
            key['unrestricted'] = bool(key['unrestricted'])
            key['expired'] = enabled and key['expires_at'] is not None and key['expires_at'] <= now
            if key['allowed_models'] is not None:
                key['allowed_models'] = json.loads(key['allowed_models'])
            keys.append(key)
        return keys

    def usage(self, key_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT model,requests,failures,input,output,cache_read,cache_write,last_used,cost_nanos,unpriced_tokens FROM usage '
                'WHERE key_id=? ORDER BY requests DESC,model', (key_id,))]

    def recent_requests(self, key_id=None):
        columns = 'SELECT key_id, model, at, outcome, status, wait_ms, duration_ms, tokens FROM request_log'
        with self.connect() as db:
            if key_id is None:
                rows = db.execute(columns + ' ORDER BY at DESC, id DESC LIMIT 30')
            else:
                rows = db.execute(columns + ' WHERE key_id=? ORDER BY at DESC, id DESC LIMIT 30', (key_id,))
            return [dict(row) for row in rows]

    def overview(self):
        now = int(time.time())
        with self.connect() as db:
            models = [dict(row) for row in db.execute('''
                SELECT model,SUM(requests) AS requests,SUM(failures) AS failures,
                       SUM(input+output+cache_read+cache_write) AS tokens,
                       SUM(cost_nanos) AS cost_nanos,SUM(unpriced_tokens) AS unpriced_tokens
                FROM usage GROUP BY model ORDER BY tokens DESC,requests DESC,model
            ''')]
            recent = dict(db.execute('''
                SELECT COUNT(*) AS requests,COALESCE(SUM(tokens),0) AS tokens,
                       COUNT(CASE WHEN outcome='failed' THEN 1 END) AS failures,
                       COUNT(CASE WHEN outcome='limited' THEN 1 END) AS limited,
                       AVG(duration_ms) AS average_ms
                FROM request_log WHERE at >= ?
            ''', (now - 86400,)).fetchone())
            pace = dict(db.execute('''
                SELECT COUNT(duration_ms) AS rpm,COALESCE(SUM(tokens),0) AS tpm
                FROM request_log WHERE at >= ?
            ''', (now - 60,)).fetchone())
            trend = [dict(row) for row in db.execute('''
                SELECT date(at,'unixepoch') AS day,COUNT(*) AS requests,SUM(tokens) AS tokens
                FROM request_log WHERE at >= ? AND duration_ms IS NOT NULL GROUP BY day ORDER BY day
            ''', ((now // 86400 - 6) * 86400,))]
        return {'models': models, 'stats': {**recent, **pace}, 'trend': trend, 'sampled_at': now}

    def analytics(self, period):
        step, count = ANALYTICS_RANGES[period]
        now = int(time.time())
        start = now - step * count
        with self.connect() as db:
            db.execute('BEGIN')
            timeline = [dict(row) for row in db.execute('''
                SELECT MIN((at-?)/?,?) AS bucket, COUNT(*) AS requests, SUM(tokens) AS tokens,
                       COUNT(CASE WHEN outcome='failed' THEN 1 END) AS failed,
                       COUNT(CASE WHEN outcome='limited' THEN 1 END) AS limited,
                       COUNT(CASE WHEN outcome='canceled' THEN 1 END) AS canceled
                FROM request_log WHERE at BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket
            ''', (start, step, count - 1, start, now))]
            outcomes = [dict(row) for row in db.execute('''
                SELECT outcome, status, COUNT(*) AS requests FROM request_log
                WHERE at BETWEEN ? AND ? GROUP BY outcome, status ORDER BY requests DESC, outcome, status
            ''', (start, now))]
            latency = {}
            for metric in ('duration_ms', 'wait_ms'):
                boundaries = ' '.join(f'WHEN {metric} < ? THEN {index}' for index in range(len(LATENCY_LIMITS)))
                rows = db.execute(f'''
                    SELECT CASE {boundaries} ELSE {len(LATENCY_LIMITS)} END AS bucket,
                           COUNT(*) AS requests, SUM({metric}) AS total_ms
                    FROM request_log WHERE at BETWEEN ? AND ? AND {metric} IS NOT NULL GROUP BY bucket
                ''', (*LATENCY_LIMITS, start, now)).fetchall()
                samples = sum(row['requests'] for row in rows)
                p95 = None
                if samples:
                    p95 = db.execute(f'''
                        SELECT {metric} FROM request_log
                        WHERE at BETWEEN ? AND ? AND {metric} IS NOT NULL ORDER BY {metric}
                        LIMIT 1 OFFSET ?
                    ''', (start, now, (samples * 95 + 99) // 100 - 1)).fetchone()[0]
                histogram = [0] * (len(LATENCY_LIMITS) + 1)
                for row in rows:
                    histogram[row['bucket']] = row['requests']
                latency[metric] = {
                    'histogram': histogram, 'samples': samples,
                    'average_ms': sum(row['total_ms'] for row in rows) / samples if samples else None,
                    'p95_ms': p95,
                }
        return {'period': period, 'start': start, 'step': step, 'count': count, 'timeline': timeline,
                'outcomes': outcomes, 'latency': latency, 'latency_limits': LATENCY_LIMITS}


class Capacity:
    """Non-blocking resource guard whose limit follows the gateway configuration."""
    def __init__(self, limit):
        self.limit = limit
        self.in_use = 0
        self.lock = threading.Lock()

    def acquire(self, *, blocking=False):
        if blocking:
            raise ValueError('Gateway capacity does not support blocking acquisition')
        with self.lock:
            limit = self.limit()
            if limit and self.in_use >= limit:
                return False
            self.in_use += 1
            return True

    def release(self):
        with self.lock:
            if not self.in_use:
                raise ValueError('Gateway capacity released without an acquisition')
            self.in_use -= 1


class RequestQueue:
    """Shared Messages capacity, FIFO waiters, per-key and global queue bounds."""
    key_idle_seconds = 60
    key_wait_seconds = 600

    def __init__(self, registry, max_wait=45, max_queue=12, per_key=2, concurrency=1, key_mode=False):
        self.registry = registry
        self.max_wait, self.max_queue, self.per_key = max_wait, max_queue, per_key
        self.changed = threading.Condition()
        self.waiters = deque()
        self.active = {}
        self.concurrency = concurrency
        self.recent = defaultdict(deque)
        self.key_mode = key_mode
        self.key_owner = None
        self.key_release = None

    def snapshot(self):
        with self.changed:
            return dict(Counter(self.active.values())), dict(Counter(key for _,key in self.waiters)), self.concurrency

    def resize(self, concurrency):
        with self.changed:
            self.concurrency = concurrency
            self.changed.notify_all()

    def wake(self):
        with self.changed:
            self.changed.notify_all()

    def set_key_mode(self, enabled):
        with self.changed:
            if self.key_mode == enabled:
                return
            self.key_mode = enabled
            owners = set(self.active.values())
            self.key_owner = next(iter(owners)) if enabled and len(owners) == 1 else None
            self.key_release = None
            self.changed.notify_all()

    def expire_key_owner(self, now):
        if self.key_release is not None and now >= self.key_release:
            self.key_owner = self.key_release = None

    def key_status(self):
        with self.changed:
            now = time.monotonic()
            self.expire_key_owner(now)
            return {
                'enabled': self.key_mode, 'key_id': self.key_owner, 'idle_seconds': self.key_idle_seconds,
                'release_in': max(0, int(self.key_release-now+0.999)) if self.key_release is not None else None,
            }

    def eligible(self, ticket, now):
        if self.concurrency and len(self.active) >= self.concurrency:
            return False
        if not self.key_mode:
            return self.waiters[0] == ticket
        self.expire_key_owner(now)
        if self.key_owner is None:
            # When enabled during concurrent traffic, let the existing batch drain.
            return not self.active and self.waiters[0] == ticket
        key = ticket[1]
        return key == self.key_owner and next(queued for queued in self.waiters if queued[1] == key) == ticket

    @contextmanager
    def slot(self, key, disconnected=lambda:False, *, on_wait=None):
        ticket = (object(), key)
        with self.changed:
            threshold = time.monotonic()-60
            self.recent = defaultdict(deque, ((k,t) for k,t in self.recent.items() if t and t[-1]>threshold))
            if (self.concurrency or self.key_mode) and (len(self.waiters) >= self.max_queue or
                                     sum(k == key for _, k in self.waiters) >= self.per_key):
                raise GatewayError('Safe Code API queue is full; retry later', 429)
            self.waiters.append(ticket)
            queued = time.monotonic()
        try:
            while True:
                with self.changed:
                    now = time.monotonic()
                    deadline = queued + (max(self.max_wait, self.key_wait_seconds) if self.key_mode else self.max_wait)
                    if not self.registry.enabled(key):
                        raise GatewayError('API Key has expired or been revoked', 401)
                    if disconnected():
                        raise ConnectionAbortedError()
                    if now>=deadline:
                        raise GatewayError('Safe Code API queue wait timed out; retry later', 429)
                    if self.eligible(ticket, now):
                        if self.registry.balance_exhausted(key):
                            raise GatewayError('Safe Code API Key balance exhausted', 402)
                        recent = self.recent[key]
                        while recent and recent[0]<=now-60:
                            recent.popleft()
                        rpm = self.registry.settings(key)
                        if rpm and len(recent)>=rpm:
                            raise GatewayError('Safe Code API Key requests per minute exceeded', 429)
                        recent.append(now)
                        if len(recent)>600:
                            recent.popleft()
                        if self.key_mode:
                            self.key_owner = key
                            self.key_release = None
                        self.active[ticket] = key
                        break
                    self.changed.wait(min(0.5, deadline-now))
                if on_wait is not None and self.key_mode:
                    on_wait()
        finally:
            with self.changed:
                self.waiters.remove(ticket)
                self.changed.notify_all()
        try:
            yield
        finally:
            with self.changed:
                del self.active[ticket]
                if self.key_mode and not self.active:
                    self.key_owner = key
                    self.key_release = time.monotonic() + self.key_idle_seconds
                self.changed.notify_all()


def peer_disconnected(connection):
    # Messages has no pipelined request. TLS reads cannot use MSG_PEEK.
    if not select.select([connection], [], [], 0)[0]:
        return False
    try:
        connection.setblocking(False)
        return not connection.recv(1)
    except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
        return False
    except OSError:
        return True
    finally:
        connection.settimeout(30)


def read_admin_line(stream):
    line = stream.readline(8193)
    if not line.endswith(b'\n') or len(line)>8192:
        raise ValueError('管理请求格式无效。')
    value = json.loads(line)
    if not isinstance(value,dict):
        raise ValueError('管理请求格式无效。')
    return value


def send_admin(root, request):
    if len(request)>8192:
        raise ValueError('管理请求过长。')
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(25)
            connection.connect(str(Path(root)/'admin.sock'))
            connection.sendall(request.rstrip(b'\n')+b'\n')
            with connection.makefile('rb') as stream:
                response=stream.readline(6*1024*1024)
    except OSError as error:
        raise ValueError('无法连接 Safe Code API 管理服务。请确认该 VPS 已更新并启动 Safe Code API。') from error
    if not response.endswith(b'\n'):
        raise ValueError('Safe Code API 管理服务未返回完整结果。')
    return json.loads(response)
