#!/usr/bin/env python3
"""为 xrdp 配置免交互登录：autorun 跳过登录框，凭据取自 Safe Code 已部署的账户。

幂等：重复执行只更新已有的键，不会重复插入。
"""
import base64
import grp
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path('/etc/xrdp/xrdp.ini')
ACCOUNT = Path('/var/lib/safe-code/account.json')
BACKUP = Path('/var/lib/safe-code/backup-xrdp.ini.autologin')
SECTION_RE = r'(?ms)^\[%s\]\s*\n(.*?)(?=^\[|\Z)'


def fail(message):
    print('ERROR: ' + message)
    sys.exit(1)


def set_key(text, section, key, value):
    """在指定段内设置 key=value；键不存在时插到段首。返回新文本。"""
    match = re.search(SECTION_RE % re.escape(section), text)
    if not match:
        fail('未找到段 [' + section + ']')
    body = match.group(1)
    key_pattern = re.compile(r'(?m)^[ \t]*' + re.escape(key) + r'=.*$')
    if key_pattern.search(body):
        new_body, count = key_pattern.subn(key + '=' + value, body, count=1)
    else:
        new_body, count = key + '=' + value + '\n' + body, 1
    if count != 1:
        fail('无法安全写入 [' + section + '] ' + key)
    return text[:match.start(1)] + new_body + text[match.end(1):]


def body_of(text, section):
    match = re.search(SECTION_RE % re.escape(section), text)
    if not match:
        fail('回读时未找到段 [' + section + ']')
    return match.group(1)


def main():
    if os.geteuid() != 0:
        fail('需要 root 权限')
    if not ACCOUNT.is_file():
        fail('未找到 ' + str(ACCOUNT) + '，请先在应用里部署工作用户')
    account = json.loads(ACCOUNT.read_text())
    username, password = account.get('username'), account.get('password')
    if not username or not password:
        fail('账户文件缺少用户名或密码')

    text = CONFIG.read_text()
    if not BACKUP.exists():
        BACKUP.write_text(text)
        BACKUP.chmod(0o600)

    token = '{base64}' + base64.b64encode(password.encode()).decode()
    updated = set_key(text, 'Globals', 'autorun', 'Xorg')
    updated = set_key(updated, 'Xorg', 'username', username)
    updated = set_key(updated, 'Xorg', 'password', token)
    if updated != text:
        CONFIG.write_text(updated)

    # 配置文件含明文凭据，最小化可读范围。注意 xrdp.service 以 User=xrdp 运行并
    # 在启动时读取本文件，600 会让服务直接起不来（日志报 could not open the
    # configuration file），因此必须把 xrdp 组授予读权限。
    try:
        os.chown(CONFIG, 0, grp.getgrnam('xrdp').gr_gid)
        CONFIG.chmod(0o640)
        mode_note = 'root:xrdp 640'
    except KeyError:
        CONFIG.chmod(0o644)
        mode_note = 'root:root 644（系统缺少 xrdp 组）'

    # 回读校验：确认三项都落在正确的段里
    check = CONFIG.read_text()
    if not re.search(r'(?m)^autorun=Xorg$', body_of(check, 'Globals')):
        fail('autorun 未生效')
    xorg = body_of(check, 'Xorg')
    if not re.search(r'(?m)^username=' + re.escape(username) + r'$', xorg):
        fail('[Xorg] username 未生效')
    if not re.search(r'(?m)^password=\{base64\}', xorg):
        fail('[Xorg] password 未生效')

    # 以 xrdp 用户身份实测可读性，避免出现"配置正确但服务起不来"
    readable = subprocess.run(
        ['su', '-s', '/bin/sh', 'xrdp', '-c', 'test -r ' + str(CONFIG)],
        capture_output=True).returncode == 0
    if not readable:
        fail('xrdp 用户无法读取 ' + str(CONFIG) + '，服务将启动失败')

    subprocess.run(['systemctl', 'restart', 'xrdp'], check=False)
    active = subprocess.run(['systemctl', 'is-active', 'xrdp'],
                            capture_output=True, text=True).stdout.strip()
    if active != 'active':
        fail('xrdp 重启后不是 active（' + (active or 'unknown') + '），配置已写入，请查看 journalctl -u xrdp')

    print('OK autorun=Xorg')
    print('OK [Xorg] username=' + username)
    print('OK [Xorg] password={base64}<' + str(len(password)) + ' 字符>')
    print('OK 权限 ' + mode_note + '，xrdp 用户可读')
    print('xrdp: ' + active)
    print('备份: ' + str(BACKUP))


if __name__ == '__main__':
    main()
