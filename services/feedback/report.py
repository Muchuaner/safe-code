#!/usr/bin/env python3
"""在服务器通过 SSH 读取报表；不暴露公网管理接口。"""
import argparse,json,sqlite3
p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('--feedback',action='store_true');p.add_argument('--per-installation',action='store_true');a=p.parse_args()
db=sqlite3.connect('file:'+a.database+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
query='SELECT event_id,created_at,version,message FROM feedback ORDER BY created_at DESC LIMIT 100' if a.feedback else 'SELECT day,COUNT(*) AS launches,COUNT(DISTINCT installation_id) AS active_installations FROM usage GROUP BY day ORDER BY day DESC LIMIT 90'
if a.per_installation and not a.feedback:query='SELECT day,installation_id,COUNT(*) AS launches FROM usage GROUP BY day,installation_id ORDER BY day DESC,launches DESC LIMIT 300'
print(json.dumps([dict(row) for row in db.execute(query)],ensure_ascii=False,indent=2))
