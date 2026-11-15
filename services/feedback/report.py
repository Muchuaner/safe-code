#!/usr/bin/env python3
"""在服务器通过 SSH 读取报表；不暴露公网管理接口。"""
import argparse,json,sqlite3,uuid

p=argparse.ArgumentParser()
p.add_argument('database')
k=p.add_mutually_exclusive_group()
k.add_argument('--feedback',action='store_true')
k.add_argument('--diagnostics',action='store_true')
k.add_argument('--summary',action='store_true',help='按操作、结果、错误分类汇总全部保留诊断')
p.add_argument('--per-installation',action='store_true')
p.add_argument('--issue',type=lambda value:str(uuid.UUID(value)),help='按问题编号查询诊断或反馈')
a=p.parse_args()
if a.issue and not (a.feedback or a.diagnostics):p.error('--issue 需要 --feedback 或 --diagnostics')
db=sqlite3.connect('file:'+a.database+'?mode=ro',uri=True)
db.row_factory=sqlite3.Row
has_context=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_context'").fetchone()
params=()
if a.summary:
    query="""SELECT version,json_extract(report,'$.operation') AS operation,
    json_extract(report,'$.outcome') AS outcome,json_extract(report,'$.errorKind') AS error_kind,
    COALESCE(json_extract(report,'$.stage'),'legacy_unknown') AS stage,COUNT(*) AS reports
    FROM diagnostics GROUP BY version,operation,outcome,error_kind,stage ORDER BY reports DESC"""
elif a.feedback or a.diagnostics:
    table='feedback' if a.feedback else 'diagnostics'
    fields='event_id,created_at,version,'+('message' if a.feedback else 'report')
    if a.issue and has_context:
        query='SELECT '+','.join('r.'+f for f in fields.split(','))+' FROM '+table+''' r LEFT JOIN report_context c ON c.event_id=r.event_id
        WHERE r.event_id=? OR json_extract(c.context,'$.operationId')=? OR json_extract(c.context,'$.correlation.operationId')=? ORDER BY r.created_at DESC LIMIT 100'''
        params=(a.issue,)*3
    else:
        query='SELECT '+fields+' FROM '+table+(' WHERE event_id=?' if a.issue else '')+' ORDER BY created_at DESC LIMIT 100'
        if a.issue:params=(a.issue,)
elif a.per_installation:
    query='SELECT day,installation_id,COUNT(*) AS launches FROM usage GROUP BY day,installation_id ORDER BY day DESC,launches DESC LIMIT 300'
else:
    query='SELECT day,COUNT(*) AS launches,COUNT(DISTINCT installation_id) AS active_installations FROM usage GROUP BY day ORDER BY day DESC LIMIT 90'
rows=[dict(row) for row in db.execute(query,params)]
if a.feedback or a.diagnostics:
    for row in rows:
        context=db.execute('SELECT context FROM report_context WHERE event_id=?',(row['event_id'],)).fetchone() if has_context else None
        row['context']=json.loads(context[0]) if context else None
        if a.diagnostics:row['report']=json.loads(row['report'])
print(json.dumps(rows,ensure_ascii=False,indent=2))
