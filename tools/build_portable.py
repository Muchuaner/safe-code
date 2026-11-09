#!/usr/bin/env python3
"""生成 Windows x64 自包含便携包。仅写本地 dist，不连接 GitHub。"""
import argparse,hashlib,json,os,shutil,subprocess,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--dotnet',default='dotnet');p.add_argument('--output-dir',type=Path);a=p.parse_args()
root=Path(__file__).resolve().parent.parent
output=a.output_dir.resolve() if a.output_dir else root/'dist'/'SafeCode-Beta-0.4.3.1-win-x64';archive=output.parent/(output.name+'.zip')
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
manifest={'version':'Beta-0.4.3.1','target':'win-x64','selfContained':True,'files':{}}
for path in sorted(output.rglob('*')):
 if path.is_file():manifest['files'][path.relative_to(output).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
(output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for path in output.rglob('*'):
  if path.is_file():z.write(path,output.name+'/'+path.relative_to(output).as_posix())
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n')
print(json.dumps({'archive':str(archive),'sha256':digest,'MiB':round(archive.stat().st_size/1024**2,1)},ensure_ascii=False))
