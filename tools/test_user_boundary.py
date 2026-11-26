#!/usr/bin/env python3
"""驗證巡檢和用戶配置不把普通工作帳戶可寫路徑提升成 root 權限。需在專用測試 VPS 執行。"""
import importlib.util,json,os,pwd,shutil,sys,uuid
from pathlib import Path
spec=importlib.util.spec_from_file_location('agent',sys.argv[1]);agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
a=agent.account();entry=pwd.getpwnam(a['username']);home=Path(entry.pw_dir);tag=uuid.uuid4().hex
root=Path('/var/lib/safe-code')/('boundary-'+tag);root.mkdir(mode=0o700)
marker=root/'marker';marker.write_text('root-owned-original');marker.chmod(0o600)
probe=home/('.safe-code-version-'+tag);link=home/('.safe-code-link-'+tag)
try:
 probe.write_text('#!/bin/sh\nif [ "$(id -u)" = 0 ]; then printf changed > '+str(marker)+'; fi\nprintf "fixture-user-%s\\n" "$(id -un)"\n');probe.chmod(0o755);os.chown(probe,entry.pw_uid,entry.pw_gid)
 assert agent.version(probe)=='fixture-user-'+a['username'],'巡檢沒有按工作用戶執行'
 link.symlink_to(marker);os.lchown(link,entry.pw_uid,entry.pw_gid)
 refused=False
 try:agent.user_write(link.name,'should-not-write')
 except Exception:refused=True
 assert refused,'未拒絕主目錄之外的配置符號連結'
 assert marker.read_text()=='root-owned-original','管理員檔案被工作用戶配置寫入覆蓋'
 assert agent.user_read(link.name)=='','普通用戶讀取到了管理員檔案'
 print(json.dumps({'passed':True,'versionRunsUnprivileged':True,'configSymlinkRejected':True,'rootFileUnchanged':True,'rootReadRejected':True}))
finally:
 probe.unlink(missing_ok=True);link.unlink(missing_ok=True);shutil.rmtree(root)
