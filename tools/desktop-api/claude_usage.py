"""Read subscription windows with Claude Code's interactive /usage command."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import selectors
import signal
import struct
import subprocess
import tempfile
import termios
import time


WINDOW_NAMES = {
    'current session': 'five_hour',
    'current week (all models)': 'seven_day',
    'current week': 'seven_day',
}
ESCAPES = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[@-_])')
COLUMNS = re.compile(r'\x1b\[[0-9]*[CG]')
USED = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%\s*(used|remaining|left)\b', re.IGNORECASE)
PROMPT = re.compile(r'(?m)^\s*[❯›]\s*(?:Try [^\n]*|Ask [^\n]*|Type [^\n]*)?$')
USAGE_COMMAND = re.compile(r'(?im)^[ \t]*(?:[❯›][ \t]*)?/usage[ \t]+(?:[–-][ \t]*)?(?:show|display|view|check)\b')
TRUST_YES = re.compile(r'(?im)^[ \t]*[❯›][ \t]*(?:1\.[ \t]*)?Yes,[ \t]+I trust this folder[ \t]*$')
TRUST_NO = re.compile(r'(?im)^[ \t]*[❯›][ \t]*(?:2\.[ \t]*)?No,[ \t]+exit[ \t]*\n[ \t]*Yes,[ \t]+I trust this folder[ \t]*$')
TRUST_OPTION = re.compile(r'(?:[❯›]\s*)?(?:[12]\.\s*)?(?:No,\s+exit|Yes,\s+I trust this folder)', re.IGNORECASE)



class UsageUnavailable(ValueError):
    pass


def terminal_text(text):
    return ESCAPES.sub('', COLUMNS.sub(' ', text)).replace('\r\r\n', '\n').replace('\r\n', '\n').replace('\r', '\n')


def usage_windows(text):
    """Keep only aggregate plan windows and percentages."""
    windows, window = {}, None
    for line in terminal_text(text).splitlines():
        label = line.strip(' │┃#*\t').rstrip(':').casefold()
        if label in WINDOW_NAMES:
            window = WINDOW_NAMES[label]
        elif label.startswith(('current ', 'extra usage', 'usage credits')):
            window = None
        if window and (match := USED.search(line)):
            percent = float(match[1])
            if 0 <= percent <= 100:
                used = percent if match[2].casefold() == 'used' else 100 - percent
                windows[window] = {'used': used}
            window = None
    return windows


def read_subscription(timeout=12):
    # TODO: Replace this direct CLI probe with a CloudCLI-backed source and remove the launcher.
    if any(os.environ.get(name) for name in (
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    )):
        raise UsageUnavailable('当前 Claude Code 使用 API 或第三方认证。')
    cli = Path.home()/'.local/bin/claude'
    if not os.access(cli, os.X_OK):
        raise UsageUnavailable('VPS 工作账户尚未安装 Claude Code。')

    deadline = time.monotonic() + timeout
    env = {**os.environ, 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'DISABLE_AUTOUPDATER': '1'}
    env['PATH'] = str(cli.parent) + ':/opt/safe-code/node/bin:' + env.get('PATH', '/usr/bin:/bin')
    with tempfile.TemporaryDirectory(prefix='safe-code-usage-') as workspace:
        auth = subprocess.run([str(cli), 'auth', 'status', '--json'], cwd=workspace, env=env,
                              capture_output=True, timeout=min(timeout, 4), check=False)
        try:
            account = json.loads(auth.stdout)
        except ValueError:
            account = {}
        if auth.returncode or not isinstance(account, dict) or account.get('loggedIn') is not True:
            raise UsageUnavailable('请先在 VPS 工作账户的 Claude Code 中登录订阅账户。')
        if account.get('authMethod') != 'claude.ai' and not (
            account.get('apiProvider') == 'firstParty' and account.get('subscriptionType')
        ):
            raise UsageUnavailable('VPS 工作账户未使用 Claude 订阅登录。')

        command = [str(cli), '--safe-mode', '--setting-sources', '', '--tools', '',
                   '--strict-mcp-config', '--permission-mode', 'dontAsk']
        master, slave = pty.openpty()
        try:
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
                child = subprocess.Popen(command, cwd=workspace, env=env, stdin=slave, stdout=slave,
                                         stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            try:
                return query_usage(master, deadline, str(Path(workspace).resolve()))
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=0.5)
                    except ProcessLookupError:
                        pass
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait()
        finally:
            os.close(master)


def query_usage(terminal, deadline, workspace):
    phase = 'startup'
    output = b''
    trust_confirmations = 0
    selecting_trust = False
    workspace_line = re.compile(r'(?m)^[ \t]*' + re.escape(workspace) + r'[ \t]*$')
    with selectors.DefaultSelector() as selector:
        selector.register(terminal, selectors.EVENT_READ)
        while time.monotonic() < deadline and selector.select(max(0, deadline - time.monotonic())):
            try:
                chunk = os.read(terminal, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            if b'\x1b[6n' in chunk:
                os.write(terminal, b'\x1b[1;1R')
            output = (output + chunk)[-128*1024:]
            text = terminal_text(output.decode('utf-8', errors='replace'))
            if phase == 'startup':
                # After selecting the private temporary folder the CLI can repaint only
                # the menu. A different path or any unrelated text requires fresh verification.
                redraw = (selecting_trust or trust_confirmations > 0) and all(
                    TRUST_OPTION.fullmatch(line.strip()) for line in text.splitlines() if line.strip())
                verified = trust_confirmations < 2 and (workspace_line.search(text.rsplit('Accessing workspace:', 1)[-1]) or redraw)
                if verified and TRUST_YES.search(text):
                    # The CLI is in the fresh empty directory created only for this query.
                    if selecting_trust:
                        time.sleep(0.12)
                    os.write(terminal, b'\r')
                    trust_confirmations += 1
                    selecting_trust, output = False, b''
                elif verified and not selecting_trust and TRUST_NO.search(text):
                    os.write(terminal, b'\x1b[B')
                    selecting_trust, output = True, b''
                elif PROMPT.search(text):
                    os.write(terminal, b'/usage')
                    phase, output = 'preview', b''
            elif phase == 'preview' and USAGE_COMMAND.search(text):
                # Enter is sent only after Claude Code shows the registered slash command.
                os.write(terminal, b'\r')
                phase, output = 'usage', b''
            elif phase == 'usage':
                windows = usage_windows(text)
                if len(windows) == 2:
                    return windows
    if phase == 'usage' and (windows := usage_windows(output.decode('utf-8', errors='replace'))):
        return windows
    if phase == 'startup':
        raise UsageUnavailable('Claude Code 未进入交互界面。请检查 VPS 上的登录或首次启动提示。')
    if phase == 'preview':
        raise UsageUnavailable('Claude Code 未显示内置 /usage 命令。请检查 VPS 上的 Claude Code 版本。')
    raise UsageUnavailable('Claude Code /usage 未返回订阅额度，请稍后手动刷新。')


if __name__ == '__main__':
    try:
        windows = read_subscription()
        print(json.dumps({'state': 'available', 'windows': windows, 'sampled_at': int(time.time())}))
    except UsageUnavailable as error:
        print(json.dumps({'state': 'unavailable', 'windows': {}, 'reason': str(error)}))
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({'state': 'unavailable', 'windows': {}, 'reason': 'VPS 上的 Claude Code 用量查询未完成。'}))
