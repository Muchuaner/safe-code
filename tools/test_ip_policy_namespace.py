#!/usr/bin/env python3
"""在两组独立 Linux 网络命名空间中验证双 IP；不改宿主默认路由和防火墙。"""
import importlib.util,json,subprocess,sys,time,uuid
spec=importlib.util.spec_from_file_location('policy',sys.argv[1]);policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
tag=uuid.uuid4().hex[:6];server='sc-server-'+tag;peer='sc-peer-'+tag;v1='scs'+tag;v2='scp'+tag
children=[]
def run(args,check=True,stdin=None):
 p=subprocess.run(args,input=stdin,text=True,capture_output=True,timeout=12)
 if check and p.returncode:raise RuntimeError(' '.join(args)+': '+p.stderr)
 return p

def inside(ns,args,check=True,stdin=None):return run(['ip','netns','exec',ns]+args,check,stdin)
def get(ns,url,interface=None):
 args=['curl','-4','-fsS','--connect-timeout','2','--max-time','3']
 if interface:args+=['--interface',interface]
 return inside(ns,args+[url],False)
try:
 for ns in [server,peer]:run(['ip','netns','add',ns]);inside(ns,['ip','link','set','lo','up'])
 run(['ip','link','add',v1,'type','veth','peer','name',v2]);run(['ip','link','set',v1,'netns',server]);run(['ip','link','set',v2,'netns',peer])
 for ns,dev,addresses in [(server,v1,['198.18.237.11/24','198.18.237.12/24']),(peer,v2,['198.18.237.1/24'])]:
  for address in addresses:inside(ns,['ip','addr','add',address,'dev',dev])
  inside(ns,['ip','link','set',dev,'up'])
 inside(peer,['ip','addr','add','203.0.113.1/32','dev','lo'])
 inside(server,['ip','route','add','default','via','198.18.237.1','dev',v1,'src','198.18.237.12'])
 code='''from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=self.client_address[0].encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',int(__import__('sys').argv[1])),Handler).serve_forever()'''
 for ns,port in [(server,22337),(peer,22336)]:children.append(subprocess.Popen(['ip','netns','exec',ns,'python3','-c',code,str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
 time.sleep(.5)
 rules=policy.rules({'ingressIp':'198.18.237.11','egressIp':'198.18.237.12','lockIngress':True,'lockEgress':True,'sshPort':22337})
 inside(server,['nft','--check','--file','-'],stdin=rules);inside(server,['nft','--file','-'],stdin=rules)
 assert get(peer,'http://198.18.237.11:22337').returncode==0,'入口连接失败'
 assert get(peer,'http://198.18.237.12:22337').returncode!=0,'出口地址意外允许管理入站'
 assert get(server,'http://203.0.113.1:22336').stdout=='198.18.237.12','默认出口错误'
 assert get(server,'http://203.0.113.1:22336','198.18.237.11').returncode!=0,'显式绑定普通 IP 意外出站'
 inside(server,['ip','addr','del','198.18.237.12/24','dev',v1])
 inside(server,['ip','route','replace','default','via','198.18.237.1','dev',v1,'src','198.18.237.11'])
 assert get(server,'http://203.0.113.1:22336').returncode!=0,'住宅出口缺失后回退到了普通 IP'
 print(json.dumps({'passed':True,'ingressOnly':True,'selectedEgressObserved':True,'alternateSourceBlocked':True,'missingEgressFailsClosed':True},ensure_ascii=False))
finally:
 for p in children:
  p.terminate()
  try:p.wait(timeout=3)
  except subprocess.TimeoutExpired:p.kill();p.wait()
 for ns in [server,peer]:run(['ip','netns','del',ns],False)
