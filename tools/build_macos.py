#!/usr/bin/env python3
"""Build a self-contained Apple Silicon app and disk image without changing installed apps or publishing."""
from verify_gateway_bundle import verify_gateway_bundle
import argparse,hashlib,json,os,plistlib,shutil,subprocess,tarfile,tempfile,urllib.request
from pathlib import Path,PurePosixPath
from build_macos_rdp import build as build_rdp, verify_library_paths
root=Path(__file__).resolve().parents[1]
frp_version='0.65.0';frp_archive='frp_'+frp_version+'_darwin_arm64.tar.gz'
frp_url='https://github.com/fatedier/frp/releases/download/v'+frp_version+'/'+frp_archive
frp_sha256='bce3badaf40cf5d3811c9eae593f3b24833bf7cb13b8b7c3f9453e94af45ded1'
p=argparse.ArgumentParser();p.add_argument('--arch',choices=['arm64'],default='arm64');p.add_argument('--dotnet',default=str(root/'.local/dotnet/dotnet') if (root/'.local/dotnet/dotnet').is_file() else shutil.which('dotnet') or 'dotnet');p.add_argument('--output-dir',type=Path,default=root/'dist/macos');p.add_argument('--sign-identity',default='-');p.add_argument('--skip-dmg',action='store_true');p.add_argument('--work-dir',type=Path,default=Path.home()/'Library/Caches/SafeCode/Builds');p.add_argument('--cmake',default='cmake');p.add_argument('--rdp-build-dir',type=Path);a=p.parse_args()
sdk=subprocess.check_output([a.dotnet,'--list-sdks'],text=True)
if not any(line.startswith('10.') for line in sdk.splitlines()):raise SystemExit('需要 .NET 10 SDK；可用 --dotnet 指定 SDK 所在的 dotnet。')
if not shutil.which('swiftc'):raise SystemExit('需要 Apple Command Line Tools / Swift。')
if not shutil.which(a.cmake):raise SystemExit('需要 CMake 编译随包分发的远程桌面组件，可用 --cmake 指定路径。')
version='0.4.21';base=a.output_dir.resolve()/a.arch;base.mkdir(parents=True,exist_ok=True);work=a.work_dir.resolve()/version/a.arch;work.mkdir(parents=True,exist_ok=True);app=work/'Safe Code.app'
rdp_payload=build_rdp(a.rdp_build_dir or a.work_dir/'rdp',a.cmake)
if app.exists():shutil.rmtree(app)
contents=app/'Contents';resources=contents/'Resources';core=resources/'Core';web=resources/'Web';macos=contents/'MacOS'
web_pages=web/'web'
for folder in [core,web_pages,macos]:folder.mkdir(parents=True)
def run(args,**kwargs):subprocess.run([str(x) for x in args],check=True,cwd=root,**kwargs)
run([a.dotnet,'publish','src/SafeCode.MacBackend','-c','Release','-r','osx-'+a.arch,'--self-contained','true','-o',core,'--nologo'])
frp_download=work/frp_archive
with urllib.request.urlopen(frp_url,timeout=120) as response:frp_data=response.read(32*1024*1024)
if hashlib.sha256(frp_data).hexdigest()!=frp_sha256:raise RuntimeError('Mac FRP 安装包与固定的官方 SHA-256 不一致。')
frp_download.write_bytes(frp_data)
frp_payload=core/'Payloads'/'frp';frp_payload.mkdir(parents=True,exist_ok=True)
frp_license=work/'frp-LICENSE'
with tarfile.open(frp_download,'r:gz') as package:
 for item in package.getmembers():
  name=PurePosixPath(item.name).name
  if name in ('frpc','LICENSE') and item.isfile():
   source=package.extractfile(item)
   if source is None:continue
   (frp_payload/'frpc' if name=='frpc' else frp_license).write_bytes(source.read())
frp_download.unlink()
if not (frp_payload/'frpc').is_file() or not frp_license.is_file():raise RuntimeError('官方 FRP 安装包中缺少 Mac frpc 或许可证。')
(frp_payload/'frpc').chmod(0o755)
for path in core.glob('*.pdb'):path.unlink()
run(['swiftc','-swift-version','5','-parse-as-library','-O','-target','arm64-apple-macos15.0',*sorted((root/'macos').glob('*.swift')),'-o',macos/'SafeCode','-framework','AppKit','-framework','WebKit','-framework','Security'])
for name in ['tray.html','index.html','styles.css','app.js','native.js','qrcode.min.js','gateway.html','gateway.css','gateway.js','gateway-charts.js','gateway-status.js','multi-gateway.js','multi-gateway.css']:shutil.copy2(root/'web'/name,web_pages/name)
(web/'assets').mkdir()
for name in ['safe-code-logo.png','server-login-tutorial.png','workspace-mascot.png','claude-client.png','codex-client.webp']:shutil.copy2(root/'assets'/name,web/'assets'/name)
with tempfile.TemporaryDirectory(prefix='safecode-icon-') as temp:
 iconset=Path(temp)/'AppIcon.iconset';iconset.mkdir()
 for size in [16,32,128,256,512]:
  for scale in [1,2]:run(['sips','-z',str(size*scale),str(size*scale),root/'assets/icons/safe-code-shortcut.png','--out',iconset/('icon_'+str(size)+'x'+str(size)+('@2x' if scale==2 else '')+'.png')],stdout=subprocess.DEVNULL)
 run(['iconutil','-c','icns',iconset,'-o',resources/'AppIcon.icns'])
info={'CFBundleIdentifier':'org.safecode.desktop','CFBundleName':'Safe Code','CFBundleDisplayName':'Safe Code','CFBundleExecutable':'SafeCode','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.4.21','CFBundleVersion':'0.4.21','CFBundleIconFile':'AppIcon','LSMinimumSystemVersion':'15.0','NSHighResolutionCapable':True,'NSAppleEventsUsageDescription':'用系统 Terminal 打开已核验服务器的 SSH 终端；不向终端传递密码。','NSHumanReadableCopyright':'Safe Code contributors. MIT License.'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
shutil.copy2(root/'LICENSE',resources/'LICENSE')
shutil.copy2(root/'third_party'/'THIRD_PARTY_NOTICES.md',resources/'THIRD_PARTY_NOTICES.md')
shutil.copy2(root/'docs/MACOS.md',resources/'使用说明.md')
legal=resources/'licenses';legal.mkdir();shutil.copy2(frp_license,legal/'FRP-0.65.0-LICENSE')
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
for path in (rdp_payload/'licenses').iterdir():
 if path.is_file():shutil.copy2(path,legal/path.name)
rdp=resources/'RDP';rdp.mkdir();rdp_executable=rdp/'sdl-freerdp'
shutil.copy2(rdp_payload/'sdl-freerdp',rdp_executable)
verify_library_paths(rdp_executable)
mit=(root/'LICENSE').read_text();(legal/'SSH.NET-2026.0.0-MIT.txt').write_text('MIT License\nCopyright Renci 2010-2026\n\n'+mit[mit.index('Permission is hereby'):])
run(['xattr','-cr',app])
# Sign Mach-O helpers before sealing the outer bundle. No hardened-runtime entitlement escalation.
magics=[b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca']
for path in [*core.rglob('*'),rdp_executable]:
 if path.is_file():
  with path.open('rb') as f:magic=f.read(4)
  if magic in magics:run(['codesign','--force','--sign',a.sign_identity,'--timestamp=none',path],stdout=subprocess.DEVNULL)
verify_gateway_bundle(web_pages, core/'SafeCode.Core.dll')
manifest={'version':version,'platform':'macOS','architecture':a.arch,'minimumSystemVersion':'15.0','signing':'ad-hoc developer build' if a.sign_identity=='-' else 'Developer ID; notarization separate','files':{}}
for folder in [resources]:
 for path in sorted(folder.rglob('*')):
  if path.is_file():manifest['files'][path.relative_to(contents).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
(resources/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
run(['xattr','-cr',app])
subprocess.run(['xattr','-xd','com.apple.FinderInfo',str(app)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
run(['codesign','--force','--sign',a.sign_identity,'--timestamp=none',app]);run(['codesign','--verify','--deep','--strict',app])
if not a.skip_dmg:
 with tempfile.TemporaryDirectory(prefix='safecode-dmg-') as temp:
  stage=Path(temp);shutil.copytree(app,stage/app.name,symlinks=True);(stage/'Applications').symlink_to('/Applications')
  image=base.parent/('SafeCode-'+version+'-macOS-'+a.arch+'.dmg')
  run(['hdiutil','create','-volname','Safe Code','-srcfolder',stage,'-format','UDZO','-ov',image])
 image.with_suffix('.dmg.sha256').write_text(hashlib.sha256(image.read_bytes()).hexdigest()+'  '+image.name+'\n')
(base/'build-report.json').write_text(json.dumps({'app':str(app),'architecture':a.arch,'version':version,'notarized':False},ensure_ascii=False,indent=2))
print(json.dumps({'app':str(app),'architecture':a.arch,'files':len(manifest['files']),'notarized':False},ensure_ascii=False))
