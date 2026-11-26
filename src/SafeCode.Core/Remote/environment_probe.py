"""Offline VPS Chrome assessment; never modifies the user's browser profile.

Risk weights follow claude-tester-six.vercel.app as reviewed on 2026-11-18.
This is a heuristic score, not an Anthropic decision or an IP trust score.
"""
import base64
import html
import json
import math
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

RULE_VERSION = 'claude-tester-2026-11-18'
SIMPLIFIED_FONTS = ['Microsoft YaHei', 'Microsoft YaHei UI', 'SimSun', 'NSimSun', 'SimHei', 'KaiTi', 'FangSong', 'DengXian', 'PingFang SC', 'Hiragino Sans GB', 'STHeiti', 'STSong', 'Songti SC', 'Source Han Sans CN', 'Source Han Sans SC', 'Noto Sans CJK SC', 'Noto Serif CJK SC', 'WenQuanYi Micro Hei', 'WenQuanYi Zen Hei']
TRADITIONAL_FONTS = ['Microsoft JhengHei', 'PMingLiU', 'MingLiU', 'DFKai-SB', 'PingFang TC', 'PingFang HK', 'Source Han Sans TW', 'Noto Sans CJK TC']
BROWSER_PROBE = r"""(()=>{
  const canvas=document.createElement('canvas').getContext('2d');
  function hasFont(name){
    return ['monospace','sans-serif','serif'].some(fallback=>{
      canvas.font='72px '+fallback;const baseline=canvas.measureText('中文字體檢測ABCabc012').width;
      canvas.font='72px "'+name+'", '+fallback;
      return Math.abs(canvas.measureText('中文字體檢測ABCabc012').width-baseline)>.5;
    });
  }
  const result={timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
    languages:Array.from(navigator.languages||[navigator.language]),
    locale:Intl.DateTimeFormat().resolvedOptions().locale,
    offset:new Date().getTimezoneOffset(),userAgent:navigator.userAgent,platform:navigator.platform,
    fontMeasurementAvailable:!!canvas,
    simplifiedFonts:canvas?SIMPLIFIED.filter(hasFont):[],traditionalFonts:canvas?TRADITIONAL.filter(hasFont):[]};
  document.getElementById('safe-code-env').textContent=JSON.stringify(result);
})();"""


def unavailable(message, username=''):
    return dict(riskScore=None, level='unavailable', message=message, signals=[], username=username, ruleVersion=RULE_VERSION)


def simplified_language(value):
    return value.startswith('zh-cn') or 'hans' in value or value == 'zh'


def score_signals(sample, username):
    timezone = sample.get('timezone', '')
    languages = [str(value).lower() for value in sample.get('languages', [])]
    locale = sample.get('locale', '')
    if not timezone or not languages or not locale or not sample.get('fontMeasurementAvailable') or not isinstance(sample.get('offset'), (int, float)):
        return unavailable('VPS瀏覽器檢測不完整', username)
    zone = 1 if timezone in ['Asia/Shanghai', 'Asia/Urumqi', 'Asia/Chongqing', 'Asia/Chungking', 'Asia/Harbin', 'Asia/Kashgar'] else .6 if timezone in ['Asia/Hong_Kong', 'Asia/Macau', 'Asia/Taipei'] else 0
    first = languages[0]
    traditional = lambda value: value.startswith(('zh-tw', 'zh-hk', 'zh-mo')) or 'hant' in value
    language = 1 if simplified_language(first) else .5 if traditional(first) else .7 if any(map(simplified_language, languages)) else .4 if any(value.startswith('zh') for value in languages) else 0
    intl = 1 if simplified_language(locale.lower()) else .5 if locale.lower().startswith('zh') else 0
    simplified = sample.get('simplifiedFonts', [])
    traditional_fonts = sample.get('traditionalFonts', [])
    fonts = min(1, .75 + .08 * len(simplified)) if simplified else .5 if traditional_fonts else 0
    device = (sample.get('platform', '') + ' ' + sample.get('userAgent', '')).lower()
    vendor, emoji = ('Apple', .25) if re.search('iphone|ipad|ipod|mac', device) else ('Google', .35) if 'android' in device else ('Microsoft', .4) if 'win' in device else ('Google', .35) if 'cros' in device else ('Linux / Other', .5) if 'linux' in device else ('Unknown', .4)
    font_names = simplified + traditional_fonts
    facts = [('timezone', '系統時區', timezone, zone, 30), ('language', '瀏覽器語言', ', '.join(languages), language, 24),
             ('fonts', '中文字體', ', '.join(font_names[:4]) + ('…' if len(font_names) > 4 else '') if font_names else '未匹配', fonts, 20),
             ('intlLocale', 'Intl區域', locale, intl, 10), ('timezoneOffset', '時區偏移', 'UTC{:+g}'.format(-sample['offset'] / 60), .7 if sample['offset'] == -480 else 0, 8),
             ('emoji', 'Emoji風格', vendor, emoji, 8)]
    signals = [dict(id=key, label=label, value=value, factor=factor, weight=weight, contribution=math.floor(factor * weight + .5)) for key, label, value, factor, weight in facts]
    score = min(100, sum(item['contribution'] for item in signals))
    level = 'low' if score <= 30 else 'medium' if score <= 60 else 'high'
    return dict(riskScore=score, level=level, message={'low': '低風險', 'medium': '中風險', 'high': '高風險'}[level], signals=signals, username=username, ruleVersion=RULE_VERSION)


def browser_context(user):
    values = {}
    system_locale = Path('/etc/default/locale')
    if system_locale.is_file():
        for line in system_locale.read_text().splitlines():
            match = re.fullmatch(r'(LANG|LANGUAGE|LC_ALL|LC_MESSAGES)=(.*)', line.strip())
            if match:
                values[match[1]] = match[2].strip('"\'')
    # Read just the locale-related variables of an existing main browser process.
    # No command line, account credentials or unrelated environment values leave the VPS.
    for entry in Path('/proc').iterdir():
        try:
            if not entry.name.isdigit() or entry.stat().st_uid != user.pw_uid:
                continue
            command = (entry / 'cmdline').read_bytes().split(b'\0')
            if not command or Path(os.fsdecode(command[0])).name not in ['chrome', 'chromium', 'chromium-browser'] or any(part.startswith(b'--type=') for part in command):
                continue
            for item in (entry / 'environ').read_bytes().split(b'\0'):
                key, separator, value = item.partition(b'=')
                if separator and key.decode(errors='ignore') in ['LANG', 'LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'TZ']:
                    values[key.decode()] = value.decode(errors='replace')
            break
        except (OSError, ValueError):
            continue
    root = Path(user.pw_dir) / '.config/google-chrome'
    profile = 'Default'
    if (root / 'Local State').is_file():
        profile = json.loads((root / 'Local State').read_text()).get('profile', {}).get('last_used', 'Default')
    if not re.fullmatch(r'Default|Profile \d+', profile):
        raise ValueError('Unsupported Chrome profile')
    preferences = root / profile / 'Preferences'
    intl = json.loads(preferences.read_text()).get('intl', {}) if preferences.is_file() else {}
    languages = intl.get('accept_languages') or intl.get('selected_languages')
    if not languages:
        languages = values.get('LANGUAGE') or values.get('LC_ALL') or values.get('LC_MESSAGES') or values.get('LANG') or 'en_US.UTF-8'
        languages = ','.join(value.split('.')[0].split('@')[0].replace('_', '-') for value in languages.split(':'))
        if languages in ['C', 'C-UTF-8', 'POSIX']:
            languages = 'en-US'
    if not isinstance(languages, str) or not re.fullmatch(r'[A-Za-z0-9,-]{2,200}', languages):
        raise ValueError('Unsupported Chrome languages')
    return values, languages


def assess(username):
    if not re.fullmatch(r'[a-z_][a-z0-9_.-]{0,31}', username or ''):
        return unavailable('請先部署工作用戶')
    try:
        user = pwd.getpwnam(username)
    except KeyError:
        return unavailable('請先部署工作用戶')
    if user.pw_uid == 0 or (os.geteuid() != 0 and os.geteuid() != user.pw_uid):
        return unavailable('無法讀取VPS工作用戶環境', username)
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome')
    if not chrome:
        return unavailable('需先安裝VPS Chrome', username)
    try:
        locale, languages = browser_context(user)
    except (OSError, ValueError, TypeError):
        return unavailable('VPS瀏覽器語言配置無法讀取', username)
    script = BROWSER_PROBE.replace('SIMPLIFIED.filter', json.dumps(SIMPLIFIED_FONTS) + '.filter').replace('TRADITIONAL.filter', json.dumps(TRADITIONAL_FONTS) + '.filter')
    page = '<!doctype html><meta charset="utf-8"><pre id="safe-code-env"></pre><script>' + script + '</script>'
    url = 'data:text/html;base64,' + base64.b64encode(page.encode()).decode()
    # All browser writes stay in a disposable RAM-backed directory when available.
    # The real profile is only read above; cookies, logins and extensions aren't loaded.
    with tempfile.TemporaryDirectory(prefix='safe-code-environment-', dir='/dev/shm' if Path('/dev/shm').is_dir() else None) as temporary:
        if os.geteuid() == 0:
            os.chown(temporary, user.pw_uid, user.pw_gid)
        env = {key: value for key, value in os.environ.items() if key in ['PATH', 'LD_LIBRARY_PATH']}
        env.update(locale)
        env.update(HOME=temporary, XDG_CONFIG_HOME=temporary + '/config', XDG_CACHE_HOME=temporary + '/cache', XDG_RUNTIME_DIR=temporary)
        args = [chrome, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-extensions',
                '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-breakpad', '--disable-crash-reporter',
                '--password-store=basic', '--metrics-recording-only', '--host-resolver-rules=MAP * ~NOTFOUND', '--proxy-server=http://127.0.0.1:9',
                '--user-data-dir=' + temporary + '/profile', '--crash-dumps-dir=' + temporary + '/crashes', '--lang=' + languages.split(',')[0], '--accept-lang=' + languages,
                '--timeout=12000', '--dump-dom', url]
        def demote():
            if os.geteuid() == 0:
                os.initgroups(username, user.pw_gid)
                os.setgid(user.pw_gid)
                os.setuid(user.pw_uid)
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, preexec_fn=demote)
        try:
            output, _ = process.communicate(timeout=18)
            if process.returncode != 0:
                return unavailable('VPS Chrome暫無法完成檢測', username)
            match = re.search(r'<pre id="safe-code-env">(.*?)</pre>', output.decode(errors='replace'), re.S)
            if not match:
                return unavailable('VPS瀏覽器未返回檢測結果', username)
            return score_signals(json.loads(html.unescape(match[1])), username)
        except (subprocess.TimeoutExpired, ValueError, TypeError):
            return unavailable('VPS環境檢測未完成', username)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=3)


if __name__ == '__main__':
    import sys
    def stop(signum, frame):
        raise InterruptedError('Environment probe cancelled')
    for name in ['SIGHUP', 'SIGTERM']:
        signal.signal(getattr(signal, name), stop)
    try:
        answer = assess(sys.argv[1] if len(sys.argv) > 1 else '')
    except Exception:
        answer = unavailable('VPS環境檢測暫不可用')
    print(json.dumps(answer, ensure_ascii=False))
