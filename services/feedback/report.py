#!/usr/bin/env python3
"""在服务器通过 SSH 读取报表；不暴露公网管理接口。"""
import argparse,json,sqlite3
p=argparse.ArgumentParser();p.add_argument('database');k=p.add_mutually_exclusive_group();k.add_argument('--feedback',action='store_true');k.add_argument('--diagnostics',action='store_true');p.add_argument('--per-installation',action='store_true');a=p.parse_args()
db=sqlite3.connect('file:'+a.database+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
query='SELECT event_id,created_at,version,message FROM feedback ORDER BY created_at DESC LIMIT 100' if a.feedback else 'SELECT day,COUNT(*) AS launches,COUNT(DISTINCT installation_id) AS active_installations FROM usage GROUP BY day ORDER BY day DESC LIMIT 90'
if a.diagnostics:query='SELECT event_id,created_at,version,report FROM diagnostics ORDER BY created_at DESC LIMIT 100'
if a.per_installation and not a.feedback and not a.diagnostics:query='SELECT day,installation_id,COUNT(*) AS launches FROM usage GROUP BY day,installation_id ORDER BY day DESC,launches DESC LIMIT 300'
rows=[dict(row) for row in db.execute(query)]
if a.diagnostics:
    for row in rows:row['report']=json.loads(row['report'])
print(json.dumps(rows,ensure_ascii=False,indent=2))
