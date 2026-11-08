#!/usr/bin/env python3
"""通过已配置的 SSH 主机运行 UTF-16 编码 PowerShell，避免嵌套引号展开。"""
import argparse,base64,subprocess,sys
p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--script');a=p.parse_args()
s=open(a.script).read() if a.script else sys.stdin.read()
s="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "+s
r=subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=15','-o','StrictHostKeyChecking=yes',a.host,'powershell -NoProfile -NonInteractive -EncodedCommand '+base64.b64encode(s.encode('utf-16le')).decode()],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
print(r.stdout.decode('utf-8',errors='replace'));sys.exit(r.returncode)
