#!/usr/bin/env python3
"""生成 Windows x64 自包含便携包。仅写本地 dist，不连接 GitHub。"""
import argparse,hashlib,json,os,re,shutil,subprocess,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--dotnet',default='dotnet');p.add_argument('--output-dir',type=Path);p.add_argument('--claude-payload',type=Path);p.add_argument('--claude-version');p.add_argument('--claude-sha256');p.add_argument('--openssh-payload',type=Path);p.add_argument('--codex-bridge-payload',type=Path);a=p.parse_args()
root=Path(__file__).resolve().parent.parent
output=a.output_dir.resolve() if a.output_dir else root/'dist'/'SafeCode-Beta-0.4.3.5-win-x64';archive=output.parent/(output.name+'.zip')
if output.exists():shutil.rmtree(output)
subprocess.run([a.dotnet,'publish',str(root/'src/SafeCode.Desktop/SafeCode.Desktop.csproj'),'-c','Release','-r','win-x64','--self-contained','true','-o',str(output),'--nologo'],cwd=root,check=True,env={**os.environ,'DOTNET_CLI_TELEMETRY_OPTOUT':'1'})
shutil.move(output/'SafeCode.Desktop.exe',output/'SafeCode.exe')
for path in output.rglob('*.pdb'):path.unlink()
# 浏览器概念稿的独立模拟客户端不随真实 Windows 客户端打包。
(output/'Web'/'client-preview.html').unlink(missing_ok=True)
for name in ['LICENSE','THIRD_PARTY_NOTICES.md']:shutil.copy2(root/name,output/name)
shutil.copy2(root/'docs/WINDOWS.md',output/'使用说明.txt')
legal=output/'licenses';legal.mkdir()
assets=json.loads((root/'src/SafeCode.Desktop/obj/project.assets.json').read_text())
packages=set(assets.get('libraries',{}))
runtime=json.loads((output/'SafeCode.Desktop.runtimeconfig.json').read_text())['runtimeOptions'].get('includedFrameworks',[])
for item in runtime:
 packages.add(item['name'].lower()+'.runtime.win-x64/'+item['version'])
cache=Path(os.environ.get('NUGET_PACKAGES',str(Path.home()/'.nuget/packages')))
for package in sorted(packages):
 if '/' not in package:continue
 name,version=package.split('/',1);source=cache/name.lower()/version
 if not source.exists():continue
 files=[x for x in source.rglob('*') if x.is_file() and ('license' in x.name.lower() or 'notice' in x.name.lower()) and x.suffix.lower() in ['.txt','.md','']]
 for path in files:
  destination=legal/(name+'-'+version+'-'+str(path.relative_to(source)).replace('\\','_').replace('/','_'))
  shutil.copy2(path,destination)
mit=(root/'LICENSE').read_text();body=mit[mit.index('Permission is hereby'):]
(legal/'SSH.NET-2026.0.0-MIT.txt').write_text('MIT License\n\nCopyright © Renci 2010-2026\n\n'+body)
if a.claude_payload:
 if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',a.claude_version or '') or not re.fullmatch('[a-f0-9]{64}',a.claude_sha256 or ''):raise SystemExit('随包 Claude Code 需要有效版本和官方 SHA256。')
 with a.claude_payload.open('rb') as f:
  digest=hashlib.sha256()
  for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
 if digest.hexdigest()!=a.claude_sha256:raise SystemExit('Claude Code 文件与官方 SHA256 不符，已停止打包。')
 bundled=output/'Payloads/claude';bundled.mkdir(parents=True)
 shutil.copy2(a.claude_payload,bundled/'linux-x64')
 (bundled/'linux-x64.json').write_text(json.dumps({'version':a.claude_version,'sha256':a.claude_sha256,'source':'https://downloads.claude.ai/claude-code-releases/'+a.claude_version+'/linux-x64/claude'}))
 (legal/'Claude-Code.txt').write_text('Claude Code 为 Anthropic 发布的未修改程序，用户须自行完成官方授权。\n许可与条款：https://code.claude.com/docs/en/legal-and-compliance\n来源：https://downloads.claude.ai/claude-code-releases/\n')
if a.openssh_payload:
 expected='b67ff8becd203502190ec42ac7787c24c2cd3325ed7700485fe7a1c83d2a7957'
 if hashlib.sha256(a.openssh_payload.read_bytes()).hexdigest()!=expected:raise SystemExit('OpenSSH 官方完整包 SHA256 不匹配。')
 bundled=output/'Payloads/openssh';bundled.mkdir(parents=True)
 shutil.copy2(a.openssh_payload,bundled/'OpenSSH-Win64.zip')
 with zipfile.ZipFile(a.openssh_payload) as source:
  for name in ['LICENSE.txt','NOTICE.txt']:(legal/('Win32-OpenSSH-'+name)).write_bytes(source.read('OpenSSH-Win64/'+name))
 (bundled/'source.json').write_text(json.dumps({'version':'10.4.0.0p1-Preview','sha256':expected,'source':'https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.4.0.0p1-Preview/OpenSSH-Win64.zip'}))
if a.codex_bridge_payload:
 if hashlib.sha256(a.codex_bridge_payload.read_bytes()).hexdigest()!='5aa7eac204d3a8273ceb25924520d62f6d705d2004a29d38aa5f6d63751b8348':raise SystemExit('Claude Codex 用户附件 SHA256 不匹配。')
 bundled=output/'Payloads/codex-claude';bundled.mkdir(parents=True)
 with zipfile.ZipFile(a.codex_bridge_payload) as source:
  prefix='Claude-Codex-VPS迁移包/app/'
  data=source.read(prefix+'codex-claude-win.exe')
  if hashlib.sha256(data).hexdigest()!='c8b2353f14afeef2a04c759d0da64d4aca301127a9e0fcdb82a8032026acd070':raise SystemExit('第三方适配器程序校验失败。')
  (bundled/'codex-claude-win.exe').write_bytes(data)
  for name in ['LICENSE.txt','THIRD_PARTY_LICENSES.txt','UPSTREAM.txt']:
   data=source.read(prefix+name);(bundled/name).write_bytes(data);(legal/('Claude-Codex-'+name)).write_bytes(data)
 (bundled/'source.json').write_bytes((root/'third_party/claude-codex-vps/SOURCE.json').read_bytes())
manifest={'version':'Beta-0.4.3.5','target':'win-x64','selfContained':True,'files':{}}
for path in sorted(output.rglob('*')):
 if path.is_file():manifest['files'][path.relative_to(output).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
(output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for path in output.rglob('*'):
  if path.is_file():z.write(path,output.name+'/'+path.relative_to(output).as_posix())
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n')
print(json.dumps({'archive':str(archive),'sha256':digest,'MiB':round(archive.stat().st_size/1024**2,1)},ensure_ascii=False))
