#!/usr/bin/env python3
"""Build self-contained macOS apps and disk images without changing installed apps or publishing."""
import argparse,hashlib,json,os,plistlib,shutil,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--arch',choices=['arm64','x64'],default='arm64');p.add_argument('--dotnet',default=str(root/'.local/dotnet/dotnet') if (root/'.local/dotnet/dotnet').is_file() else shutil.which('dotnet') or 'dotnet');p.add_argument('--output-dir',type=Path,default=root/'dist/macos');p.add_argument('--claude-payload',type=Path);p.add_argument('--sign-identity',default='-');p.add_argument('--skip-dmg',action='store_true');p.add_argument('--work-dir',type=Path,default=Path.home()/'Library/Caches/SafeCode/Builds');a=p.parse_args()
sdk=subprocess.check_output([a.dotnet,'--list-sdks'],text=True)
if not any(line.startswith('10.') for line in sdk.splitlines()):raise SystemExit('需要 .NET 10 SDK；可用 --dotnet 指定 SDK 所在的 dotnet。')
if not shutil.which('swiftc'):raise SystemExit('需要 Apple Command Line Tools / Swift。')
version='Beta-0.4.4.1';base=a.output_dir.resolve()/a.arch;base.mkdir(parents=True,exist_ok=True);work=a.work_dir.resolve()/version/a.arch;work.mkdir(parents=True,exist_ok=True);app=work/'Safe Code.app'
if app.exists():shutil.rmtree(app)
contents=app/'Contents';resources=contents/'Resources';core=resources/'Core';web=resources/'Web';macos=contents/'MacOS'
for folder in [core,web,macos]:folder.mkdir(parents=True)
def run(args,**kwargs):subprocess.run([str(x) for x in args],check=True,cwd=root,**kwargs)
run([a.dotnet,'publish','src/SafeCode.MacBackend','-c','Release','-r','osx-'+a.arch,'--self-contained','true','-o',core,'--nologo'])
for path in core.glob('*.pdb'):path.unlink()
run(['swiftc','-swift-version','5','-parse-as-library','-O','-target',('arm64' if a.arch=='arm64' else 'x86_64')+'-apple-macos15.0','macos/SafeCode.swift','-o',macos/'SafeCode','-framework','AppKit','-framework','WebKit','-framework','Security'])
for name in ['index.html','styles.css','app.js','native.js','qrcode.min.js','guide.html','guide.js']:shutil.copy2(root/name,web/name)
(web/'assets').mkdir();shutil.copy2(root/'assets/safe-code-logo.png',web/'assets/safe-code-logo.png')
with tempfile.TemporaryDirectory(prefix='safecode-icon-') as temp:
 iconset=Path(temp)/'AppIcon.iconset';iconset.mkdir()
 for size in [16,32,128,256,512]:
  for scale in [1,2]:run(['sips','-z',str(size*scale),str(size*scale),root/'assets/icons/safe-code-shortcut.png','--out',iconset/('icon_'+str(size)+'x'+str(size)+('@2x' if scale==2 else '')+'.png')],stdout=subprocess.DEVNULL)
 run(['iconutil','-c','icns',iconset,'-o',resources/'AppIcon.icns'])
info={'CFBundleIdentifier':'org.safecode.desktop','CFBundleName':'Safe Code','CFBundleDisplayName':'Safe Code','CFBundleExecutable':'SafeCode','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.4.4','CFBundleVersion':'0.4.4.1','CFBundleIconFile':'AppIcon','LSMinimumSystemVersion':'15.0','NSHighResolutionCapable':True,'NSAppleEventsUsageDescription':'用系统 Terminal 打开已核验服务器的 SSH 终端；不向终端传递密码。','NSHumanReadableCopyright':'Safe Code contributors. MIT License.'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
for name in ['LICENSE','THIRD_PARTY_NOTICES.md']:shutil.copy2(root/name,resources/name)
shutil.copy2(root/'docs/MACOS.md',resources/'使用说明.md')
legal=resources/'licenses';legal.mkdir()
# Preserve the license documents from the exact restored packages and bundled runtime.
cache=Path(os.environ.get('NUGET_PACKAGES',str(Path.home()/'.nuget/packages')))
assets=json.loads((root/'src/SafeCode.MacBackend/obj/project.assets.json').read_text());packages=set(assets.get('libraries',{}))
for item in json.loads((core/'SafeCode.MacBackend.runtimeconfig.json').read_text())['runtimeOptions'].get('includedFrameworks',[]):
 packages.add(item['name'].lower()+'.runtime.osx-'+a.arch+'/'+item['version'])
for package in packages:
 if '/' not in package:continue
 name,ver=package.split('/',1);source=cache/name.lower()/ver
 if not source.exists():continue
 for path in source.rglob('*'):
  if path.is_file() and ('license' in path.name.lower() or 'notice' in path.name.lower()) and path.suffix.lower() in ['.txt','.md','']:
   shutil.copy2(path,legal/(name+'-'+ver+'-'+str(path.relative_to(source)).replace('/','_')))
mit=(root/'LICENSE').read_text();(legal/'SSH.NET-2026.0.0-MIT.txt').write_text('MIT License\nCopyright Renci 2010-2026\n\n'+mit[mit.index('Permission is hereby'):])
if a.claude_payload:
 digest=hashlib.sha256(a.claude_payload.read_bytes()).hexdigest()
 if digest!='86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada':raise SystemExit('Claude Code official payload checksum mismatch')
 payload=core/'Payloads/claude';payload.mkdir(parents=True);shutil.copy2(a.claude_payload,payload/'linux-x64')
 (payload/'linux-x64.json').write_text(json.dumps({'version':'2.1.311','sha256':digest,'source':'https://downloads.claude.ai/claude-code-releases/2.1.311/linux-x64/claude'}))
 (legal/'Claude-Code.txt').write_text('Unmodified Anthropic Claude Code Linux x64 2.1.311; official authorization required.\nhttps://code.claude.com/docs/en/legal-and-compliance\n')
run(['xattr','-cr',app])
# Sign Mach-O helpers before sealing the outer bundle. No hardened-runtime entitlement escalation.
magics=[b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca']
for path in core.rglob('*'):
 if path.is_file():
  with path.open('rb') as f:magic=f.read(4)
  if magic in magics:run(['codesign','--force','--sign',a.sign_identity,'--timestamp=none',path],stdout=subprocess.DEVNULL)
manifest={'version':version,'platform':'macOS','architecture':a.arch,'minimumSystemVersion':'15.0','signing':'ad-hoc developer build' if a.sign_identity=='-' else 'Developer ID; notarization separate','files':{}}
for folder in [resources]:
 for path in sorted(folder.rglob('*')):
  if path.is_file():manifest['files'][path.relative_to(contents).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
(resources/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
run(['xattr','-cr',app])
run(['codesign','--force','--sign',a.sign_identity,'--timestamp=none',app]);run(['codesign','--verify','--deep','--strict',app])
if not a.skip_dmg:
 with tempfile.TemporaryDirectory(prefix='safecode-dmg-') as temp:
  stage=Path(temp);shutil.copytree(app,stage/app.name,symlinks=True);(stage/'Applications').symlink_to('/Applications')
  image=base.parent/('SafeCode-'+version+'-macOS-'+a.arch+'.dmg')
  run(['hdiutil','create','-volname','Safe Code','-srcfolder',stage,'-format','UDZO','-ov',image])
 image.with_suffix('.dmg.sha256').write_text(hashlib.sha256(image.read_bytes()).hexdigest()+'  '+image.name+'\n')
(base/'build-report.json').write_text(json.dumps({'app':str(app),'architecture':a.arch,'version':version,'notarized':False},ensure_ascii=False,indent=2))
print(json.dumps({'app':str(app),'architecture':a.arch,'files':len(manifest['files']),'notarized':False},ensure_ascii=False))
