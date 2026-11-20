"""Read subscription windows on demand via Claude Code's official /usage command."""

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


WINDOW_NAMES = {
    'current session': 'five_hour',
    'current week (all models)': 'seven_day',
    'current week': 'seven_day',
}
TERMINAL_COLOR = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
USED = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%\s+used\b', re.IGNORECASE)


def usage_windows(text):
    """Keep only aggregate plan windows and percentages, never raw CLI output."""
    windows, window = {}, None
    for line in TERMINAL_COLOR.sub('', text).splitlines():
        label = line.strip(' #*\t').rstrip(':').casefold()
        if label in WINDOW_NAMES:
            window = WINDOW_NAMES[label]
        elif label.startswith(('current ', 'extra usage', 'usage credits')):
            window = None
        if window and (match := USED.search(line)):
            used = float(match[1])
            if 0 <= used <= 100:
                windows[window] = {'used': used}
            window = None
    return windows


def read_subscription(timeout=6):
    """Only send /usage after the uncustomized CLI advertises it for this session."""
    if any(os.environ.get(name) for name in (
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    )):
        return {}
    cli = Path.home()/'.local/bin/claude'
    if not cli.is_file():
        return {}
    command = [str(cli), '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
               '--verbose', '--safe-mode', '--setting-sources', '', '--tools', '', '--strict-mcp-config',
               '--permission-mode', 'dontAsk', '--no-session-persistence', '--settings', '{"disableAllHooks":true}']
    child = subprocess.Popen(command, cwd=Path.home(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        def send(value):
            child.stdin.write(json.dumps(value).encode()+b'\n')
            child.stdin.flush()

        send({'type': 'control_request', 'request_id': 'usage-init', 'request': {'subtype': 'initialize'}})
        deadline = time.monotonic()+timeout
        pending, total, sent, text = b'', 0, False, []
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline-time.monotonic())):
                    break
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 512*1024:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not sent and event.get('type') == 'control_response':
                        response = event.get('response', {})
                        if response.get('request_id') != 'usage-init':
                            continue
                        info = response.get('response', {})
                        account = info.get('account', {})
                        if account.get('apiProvider') != 'firstParty' or not account.get('subscriptionType'):
                            return {}
                        if not any(item.get('name') == 'usage' for item in info.get('commands', [])):
                            return {}
                        send({'type': 'user', 'message': {'role': 'user', 'content': '/usage'}})
                        sent = True
                    elif sent and event.get('type') == 'assistant':
                        text.extend(block['text'] for block in event.get('message', {}).get('content', [])
                                    if block.get('type') == 'text' and isinstance(block.get('text'), str))
                    elif sent and event.get('type') == 'result':
                        result = event.get('result')
                        if isinstance(result, str) and not event.get('is_error'):
                            text.append(result)
                        return usage_windows('\n'.join(text))
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        child.stdin.close()
        child.stdout.close()
    return {}
