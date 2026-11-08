#!/usr/bin/env python3
"""Safe Code 独立 IPv4 出入口策略。仅管理专用 nft 表与自己的默认路由首选源。"""
import ipaddress,json,os,re,secrets,shlex,subprocess,sys,time
from pathlib import Path
ROOT=Path('/var/lib/safe-code')
TABLE='safe_code_ip'
MARKER='Safe Code managed'
def run(args,check=True,stdin=None):
    p=subprocess.run(args,input=stdin,text=True,capture_output=True,timeout=25)
    if check and p.returncode:raise RuntimeError((p.stderr or p.stdout)[-1200:])
    return p

def save(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(p.name+'.new')
    t.write_text(json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value);t.chmod(0o600);os.replace(t,p)

def assigned():
    result={}
    for interface in json.loads(run(['ip','-j','-4','address','show']).stdout):
        for addr in interface.get('addr_info',[]):
            if addr.get('scope')=='global':result[addr['local']]=interface['ifname']
    return result

def route():
    lines=run(['ip','-4','route','show','default']).stdout.strip().splitlines()
    if len(lines)!=1:raise RuntimeError('当前网络有多个或没有默认路由，请先由管理员确认网络配置。')
    tokens=shlex.split(lines[0]);data=json.loads(run(['ip','-j','-4','route','show','default']).stdout)[0]
    return {'tokens':tokens,'dev':data.get('dev',''),'source':data.get('prefsrc','')}

def existing_table():
    p=run(['nft','-j','list','table','inet',TABLE],False)
    if p.returncode:return None
    objects=json.loads(p.stdout).get('nftables',[])
    table=next((x['table'] for x in objects if 'table' in x),{})
    if table.get('comment')!=MARKER:raise RuntimeError('同名防火墙表并非本软件创建，拒绝覆盖。')
    return run(['nft','list','table','inet',TABLE]).stdout

def rules(policy):
    ingress=str(ipaddress.IPv4Address(policy['ingressIp']));egress=str(ipaddress.IPv4Address(policy['egressIp']));port=int(policy['sshPort'])
    incoming=''
    if policy.get('lockIngress'):
        incoming=f'''ct state established,related accept
        iifname "lo" accept
        ip daddr != {ingress} tcp dport {port} drop
        meta nfproto ipv6 tcp dport {port} drop'''
    outgoing=''
    if policy.get('lockEgress'):
        outgoing=f'''oifname "lo" accept
        ct direction reply accept
        meta nfproto ipv6 ip6 daddr {{ fe80::/10, ff00::/8 }} accept
        meta nfproto ipv6 drop
        ip saddr != {egress} drop'''
    return f'''table inet {TABLE} {{
    comment "{MARKER}"
    chain management_input {{
        type filter hook input priority -10; policy accept;
        {incoming}
    }}
    chain egress_guard {{
        type filter hook postrouting priority 110; policy accept;
        {outgoing}
    }}
}}
'''

def snapshot():
    return {'route':route(),'table':existing_table(),'policy':json.loads((ROOT/'ip-policy.json').read_text()) if (ROOT/'ip-policy.json').exists() else None}

def valid_policy(options):
    ingress=str(ipaddress.IPv4Address(options.get('ingressIp','')));egress=str(ipaddress.IPv4Address(options.get('egressIp','')))
    addresses=assigned()
    if ingress not in addresses or egress not in addresses:raise RuntimeError('入口和出口 IP 必须已由服务商分配并绑定到本机，不能只填入一个未分配的地址。')
    port=int(options.get('sshPort',22))
    if not 1<=port<=65535:raise RuntimeError('SSH 端口无效。')
    r=route()
    if addresses[egress]!=r['dev']:raise RuntimeError('出口与默认路由不在同一网卡，本版本拒绝自动修改此类多网卡网络。')
    return {'ingressIp':ingress,'egressIp':egress,'lockIngress':bool(options.get('lockIngress')),'lockEgress':bool(options.get('lockEgress')),'sshPort':port}

def replace_source(policy,baseline):
    tokens=route()['tokens']
    if 'src' in tokens:
        i=tokens.index('src');tokens=tokens[:i]+tokens[i+2:]
    source=policy['egressIp'] if policy.get('lockEgress') else baseline.get('source','')
    if source:tokens+=['src',source]
    run(['ip','-4','route','replace']+tokens)

def apply_rules(policy):
    previous=existing_table()
    batch=('delete table inet '+TABLE+'\n' if previous is not None else '')+rules(policy)
    run(['nft','--check','--file','-'],stdin=batch)
    run(['nft','--file','-'],stdin=batch)

def prepare(options):
    if not Path('/usr/sbin/nft').exists():raise RuntimeError('请先安装 nftables 组件。')
    policy=valid_policy(options)
    pending=list(ROOT.glob('ip-pending-*.json'))
    if pending:raise RuntimeError('已有网络变更正在验证，等待自动回退或完成后重试。')
    token=secrets.token_hex(12)
    before=snapshot()
    save(ROOT/('ip-pending-'+token+'.json'),{'policy':policy,'before':before,'created':time.time()})
    if not (ROOT/'ip-baseline.json').exists():save(ROOT/'ip-baseline.json',before['route'])
    return {'token':token,'policy':policy}

def load_pending(token):
    if not re.fullmatch('[a-f0-9]{24}',token):raise RuntimeError('网络变更标识无效。')
    path=ROOT/('ip-pending-'+token+'.json')
    if not path.exists():raise RuntimeError('网络变更已过期或已回退，请重新检查。')
    return path,json.loads(path.read_text())

def apply(token):
    path,record=load_pending(token);policy=valid_policy(record['policy'])
    # 回退脚本保存在本机 root 管理目录，不依赖 SSH 会话存活。
    target=Path('/opt/safe-code/network.py');target.write_text(Path(__file__).read_text());target.chmod(0o700)
    unit='safe-code-ip-rollback-'+token
    run(['systemd-run','--quiet','--unit='+unit,'--on-active=120s','/usr/bin/python3',str(target),'rollback',token])
    try:
        apply_rules(policy)
        replace_source(policy,json.loads((ROOT/'ip-baseline.json').read_text()))
        record['applied']=True;save(path,record)
        return {'token':token,'policy':policy,'rollbackSeconds':120}
    except Exception:
        rollback(token);raise

def rollback(token):
    try:path,record=load_pending(token)
    except RuntimeError:return {'rolledBack':False}
    before=record['before']
    run(['ip','-4','route','replace']+before['route']['tokens'])
    now=existing_table();batch='delete table inet '+TABLE+'\n' if now is not None else ''
    if before.get('table'):batch+=before['table']
    if batch:run(['nft','--file','-'],stdin=batch)
    run(['systemctl','stop','safe-code-ip-rollback-'+token+'.timer'],False)
    path.unlink(missing_ok=True)
    return {'rolledBack':True}

def commit(token):
    path,record=load_pending(token)
    if not record.get('applied'):raise RuntimeError('网络策略尚未应用。')
    policy=valid_policy(record['policy'])
    actual=run(['curl','-4','-fsS','--connect-timeout','5','--max-time','12','https://api.ipify.org']).stdout.strip()
    if policy.get('lockEgress') and actual!=policy['egressIp']:raise RuntimeError('出口验证未通过：外部看到的 IP 与所选住宅出口不一致，将自动回退。')
    save(ROOT/'ip-policy.json',policy)
    unit='''[Unit]\nDescription=Safe Code assigned IP policy\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/python3 /opt/safe-code/network.py boot\n[Install]\nWantedBy=multi-user.target\n'''
    unit_path=Path('/etc/systemd/system/safe-code-ip-policy.service')
    if unit_path.exists() and 'Safe Code assigned IP policy' not in unit_path.read_text():raise RuntimeError('同名启动服务并非本软件创建。')
    save(unit_path,unit);unit_path.chmod(0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','safe-code-ip-policy.service'])
    run(['systemctl','stop','safe-code-ip-rollback-'+token+'.timer'],False)
    path.unlink();return {'policy':policy,'observedEgress':actual,'committed':True}

def boot():
    policy=json.loads((ROOT/'ip-policy.json').read_text())
    # 即使住宅地址在开机时缺失，也先加载阻断规则，不能静默回退到其他出口。
    apply_rules(policy)
    valid_policy(policy)
    replace_source(policy,json.loads((ROOT/'ip-baseline.json').read_text()))

if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('需要 root 权限。')
    if sys.argv[1]=='rollback':print(json.dumps(rollback(sys.argv[2])))
    elif sys.argv[1]=='boot':boot()
