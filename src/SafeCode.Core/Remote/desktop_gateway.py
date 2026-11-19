#!/usr/bin/env python3
"""Claude Desktop Messages API → 现有 VPS 桥接守护进程 → 原版 Claude Code。

正式环境的模型请求只由远端 claude 进程发出。214 的显式测试配置可将判定请求转到本机模拟服务；不读取上游模型密钥。
状态、会话及正文只在工作用户范围内；HTTP 仅回环监听，需独立随机 token。
"""
import argparse
import base64
import copy
from contextlib import closing
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import re
import select
import sqlite3
import signal
import shlex
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_REQUEST = 8 * 1024 * 1024
MESSAGES_PATH = '/v1/messages'
COUNT_TOKENS_PATH = MESSAGES_PATH + '/count_tokens'
MODEL = re.compile(r'^(?:claude-[a-z0-9]+(?:-[a-z0-9]+)*|fable|opus|sonnet|haiku)(?:\[1m\])?$')
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max')


def native_model_catalog(home=None, timeout=25):
    """Read the installed CLI's initialize response, without sending a model prompt.

    Do not export account/identity data from that response. Versions and effort
    capabilities come from the CLI rather than a fixed list in the desktop app.
    """
    home = Path(home) if home is not None else Path.home()
    child = subprocess.Popen(
        [str(home/'.local/bin/claude'), '-p', '--input-format', 'stream-json',
         '--output-format', 'stream-json', '--verbose', '--permission-mode', 'dontAsk',
         '--tools', '', '--strict-mcp-config', '--no-session-persistence',
         '--settings', '{"disableAllHooks":true}'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=str(home), start_new_session=True)
    try:
        child.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'safe-code-models',
                                     'request': {'subtype': 'initialize'}}).encode()+b'\n')
        child.stdin.flush()
        deadline = time.monotonic()+timeout
        pending = b''
        total = 0
        while time.monotonic() < deadline:
            if not select.select([child.stdout], [], [], max(0, deadline-time.monotonic()))[0]:break
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:break
            total += len(chunk)
            if total > 4*1024*1024:raise GatewayError('Native model catalog response too large')
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if not line.strip():continue
                event = json.loads(line)
                response = event.get('response', {})
                if event.get('type') == 'control_response' and response.get('request_id') == 'safe-code-models':
                    rows = response.get('response', {}).get('models', [])
                    return model_catalog(rows, home)
        raise GatewayError('Native Claude Code model discovery did not complete')
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL);child.wait()
        child.stdin.close();child.stdout.close()


def model_catalog(rows, home):
    models = {}
    aliases = {}
    for row in rows:
        identity = row.get('resolvedModel', '')
        if not isinstance(identity, str) or not identity.startswith('claude-') or not MODEL.fullmatch(identity):continue
        alias = row.get('value', '')
        if alias == 'default' and any(r.get('resolvedModel') == identity and r.get('value') != 'default' for r in rows):continue
        long_context = identity.endswith('[1m]') or alias.endswith('[1m]')
        identity = identity.removesuffix('[1m]')
        aliases[alias.removesuffix('[1m]')] = identity
        if identity in models:
            models[identity]['supports_1m'] |= long_context
            continue
        family = next((f for f in ('opus', 'fable', 'sonnet', 'haiku', 'mythos') if identity.startswith('claude-'+f+'-')), None)
        levels = [e for e in EFFORT_ORDER if e in row.get('supportedEffortLevels', [])]
        models[identity] = {'id': identity, 'type': 'model', 'display_name': row.get('displayName', identity),
                            'description': row.get('description', ''), 'supported_effort_levels': levels,
                            'anthropic_family_tier': family, 'is_family_default': family is not None,
                            'supports_1m': long_context}
    # This cache contains additional model choices, not authentication data.
    # Only expose entries explicitly advertised by the installed CLI/account.
    try:extras = json.loads((Path(home)/'.claude.json').read_text()).get('additionalModelOptionsCache', []) or []
    except (OSError, ValueError):extras = []
    for row in extras:
        identity = row.get('value', '')
        if not isinstance(identity, str) or not MODEL.fullmatch(identity):continue
        base = identity.removesuffix('[1m]')
        base = aliases.get(base, base)
        if base in models and identity.endswith('[1m]'):
            models[base]['supports_1m'] = True
        elif identity.startswith('claude-') and identity not in models:
            models[identity] = {'id': identity, 'type': 'model', 'display_name': row.get('label', identity),
                                'description': row.get('description', ''), 'supported_effort_levels': [],
                                'anthropic_family_tier': None, 'is_family_default': False, 'supports_1m': identity.endswith('[1m]')}
    if not models:raise GatewayError('Native Claude Code returned no model choices')
    return {'data': list(models.values()), 'has_more': False,
            'first_id': next(iter(models)), 'last_id': next(reversed(models))}


class GatewayError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class StreamMessage:
    """保留 CLI 流中的真实部分响应，以正确处理 max_tokens 截断。"""
    def __init__(self):
        self.content={};self.json_parts={};self.stop=None
        self.started = False
    def feed(self,event):
        kind=event.get('type');index=event.get('index')
        if kind=='message_start':
            self.content={};self.json_parts={};self.stop=None
            self.started = True
        elif kind=='content_block_start':self.content[index]=copy.deepcopy(event['content_block'])
        elif kind=='content_block_delta' and index in self.content:
            block=self.content[index];delta=event.get('delta',{})
            if delta.get('type')=='text_delta':block['text']=block.get('text','')+delta.get('text','')
            elif delta.get('type')=='thinking_delta':block['thinking']=block.get('thinking','')+delta.get('thinking','')
            elif delta.get('type')=='signature_delta':block['signature']=block.get('signature','')+delta.get('signature','')
            elif delta.get('type')=='input_json_delta':self.json_parts[index]=self.json_parts.get(index,'')+delta.get('partial_json','')
        elif kind=='message_delta':self.stop=event.get('delta',{}).get('stop_reason')
    def partial(self):
        result=[]
        for index,block in sorted(self.content.items()):
            if block.get('type')=='thinking' and not block.get('signature'):continue
            if index in self.json_parts:
                try:block={**block,'input':json.loads(self.json_parts[index])}
                except ValueError:continue
            result.append(block)
        return result


def blocks(value):
    if isinstance(value, str):
        return [{'type': 'text', 'text': value}] if value else []
    if not isinstance(value, list):
        raise GatewayError('Invalid message content', 400)
    return copy.deepcopy(value)


def normalized(messages):
    """忽略缓存提示及文本分块差异，不丢失角色、工具 ID、工具参数和图片。"""
    out = []
    for message in messages:
        content = []
        for item in blocks(message.get('content', [])):
            item.pop('cache_control', None)
            if item.get('type') == 'text' and content and content[-1].get('type') == 'text':
                content[-1]['text'] += item.get('text', '')
            else:
                content.append(item)
        out.append({'role': message.get('role'), 'content': content})
    return out


def signature(messages):
    return hashlib.sha256(json.dumps(normalized(messages), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_request(data):
    if not isinstance(data, dict) or not MODEL.fullmatch(str(data.get('model', ''))):
        raise GatewayError('A native Claude model identifier is required', 400)
    messages = data.get('messages')
    if isinstance(messages,list) and any(isinstance(m,dict) and m.get('role')=='system' for m in messages):
        system=blocks(data.get('system',''))
        for message in messages:
            if message.get('role')=='system':system.extend(blocks(message.get('content','')))
        data={**data,'system':system,'messages':[m for m in messages if m.get('role')!='system']}
        messages=data['messages']
    if not isinstance(messages, list) or not messages or messages[-1].get('role') != 'user':
        raise GatewayError('Messages must end with a user turn', 400)
    for message in messages:
        if message.get('role') not in ('user', 'assistant'):
            raise GatewayError('Unsupported message role', 400)
        blocks(message.get('content', []))
    tools = data.get('tools', [])
    if not isinstance(tools, list) or len(tools) > 128:
        raise GatewayError('Too many tools', 400)
    names = set()
    for tool in tools:
        if tool.get('type') not in (None, 'custom'):
            raise GatewayError('Provider-side server tools are not supported by this CLI bridge', 400)
        name = tool.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in names or not isinstance(tool.get('input_schema'), dict):
            raise GatewayError('Only unique client-defined tools are supported', 400)
        names.add(name)
    effort = data.get('output_config', {}).get('effort')
    if effort is not None and effort not in EFFORTS:
        raise GatewayError('Unsupported effort', 400)
    choice = data.get('tool_choice', {}).get('type', 'auto')
    if choice not in ('auto', 'none'):
        raise GatewayError('Forced tool selection is not supported by this CLI bridge', 400)
    # CLI 没有等价的采样/停止序列控制，拒绝而非悄悄改变其语义。
    if data.get('stop_sequences') or data.get('temperature') not in (None, 1) or data.get('top_k') is not None or data.get('top_p') is not None:
        raise GatewayError('Sampling overrides and stop sequences are not supported by this CLI bridge', 400)
    maximum = data.get('max_tokens')
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 128000:
        raise GatewayError('max_tokens must be between 1 and 128000', 400)
    return data


def classifier_test_route(home=None, hostname=None):
    # Never activate test transport on a production host, even if a config was copied there.
    if (hostname or os.uname().nodename) != 'tom666':return None
    path = (Path(home) if home else Path.home())/'.config/safe-code/desktop-classifier-test.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise GatewayError('Invalid local classifier test configuration')
    config = json.loads(path.read_text())
    if config.get('kind') != '214-classifier-simulation' or config.get('port') != 18898 or config.get('model') != 'claude-sonnet-5' or not isinstance(config.get('token'),str) or not config['token']:
        raise GatewayError('Invalid local classifier test route')
    return config


def desktop_auto_check(data):
    """The Desktop-managed CLI can use the active chat model for both Auto stages."""
    if not isinstance(data, dict) or data.get('tools') or data.get('stream'):
        return False
    model = data.get('model')
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        return False
    # The CLI adds 2048 tokens to the stage budget for some model families.
    budget = data.get('max_tokens')
    stops = data.get('stop_sequences')
    if stops:
        if stops not in (['</severity>'], ['</block>']) or budget not in (64, 2112):
            return False
    elif budget not in (256, 2304, 8192, 10240):
        return False
    # Only inspect the classifier envelope, never the command to be assessed.
    # The CLI emits the delimiters as separate text blocks (sometimes separate messages).
    messages = data.get('messages')
    if not isinstance(messages, list):
        return False
    opened = closed = False
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block.get('text')
            if not isinstance(text, str):
                continue
            if closed and ('<block>' in text or '<severity>' in text):
                return True
            if opened and text.strip() == '</transcript>':
                closed = True
            elif text.strip() == '<transcript>':
                opened = True
    return False


def read_test_stream(response):
    message = None
    stream = StreamMessage()
    total = 0
    while line := response.readline(MAX_REQUEST + 1):
        total += len(line)
        if total > MAX_REQUEST:
            break
        if not line.startswith(b'data:'):
            continue
        event = json.loads(line[5:])
        kind = event.get('type')
        stream.feed(event)
        if kind == 'message_start':
            message = event['message']
        elif kind == 'message_delta' and message is not None:
            message['usage'].update(event.get('usage', {}))
            message['stop_reason'] = event['delta']['stop_reason']
            message['stop_sequence'] = event['delta'].get('stop_sequence')
        elif kind == 'message_stop' and message is not None:
            message['content'] = stream.partial()
            return message
        elif kind == 'error':
            break
    raise GatewayError('Local test model returned an incomplete stream', 502)


def simulator_request(route, path, payload, *, stream=False, headers=None):
    client = http.client.HTTPConnection('127.0.0.1', route['port'], timeout=30 if path == COUNT_TOKENS_PATH else 120)
    try:
        client.request('POST', path, json.dumps(payload).encode(), {
            **(headers or {}), 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + route['token'],
        })
        response = client.getresponse()
        if response.status != 200:
            if path == COUNT_TOKENS_PATH and response.status in (400, 413, 429):
                raise GatewayError('Local simulator rejected token counting request', response.status)
            raise GatewayError('Local test model transport unavailable; no fallback', 502)
        if stream:
            return read_test_stream(response)
        body = response.read(MAX_REQUEST+1)
        if len(body) > MAX_REQUEST:
            raise GatewayError('Local test model response is too large', 502)
        return json.loads(body)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        raise GatewayError('Local test model transport unavailable; no fallback', 502)
    finally:client.close()


def test_messages_request(route, payload):
    result = simulator_request(route, MESSAGES_PATH, payload, stream=bool(payload.get('stream')))
    if not isinstance(result, dict) or result.get('type') != 'message' or result.get('model') != payload['model']:
        raise GatewayError('Invalid local test model response', 502)
    return result


def run_test_classifier(route, data):
    if data.get('tools'):raise GatewayError('The test classifier model accepts no tools',400)
    requested_model = data['model']
    # Keep the native model and exact controls from Desktop up to the simulator.
    # The compatibility route also accepts the bridge's legacy model aliases.
    payload = dict(data)
    if not requested_model.startswith('claude-'):
        payload['model'] = route['model']
    if data.get('stream'):
        payload['stream'] = False
    result = test_messages_request(route, payload)
    if result.get('stop_reason') not in ('end_turn','max_tokens','stop_sequence'):
        raise GatewayError('Invalid local test classifier response',502)
    if any(b.get('type')=='tool_use' for b in result.get('content',[])):
        raise GatewayError('Unexpected classifier tool response',502)
    return {**result, 'model': requested_model}


def native_web_search_request(data):
    if not isinstance(data, dict):
        return False
    tools = data.get('tools')
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
        return False
    tool = tools[0]
    return tool.get('type') == 'web_search_20250305' and tool.get('name') == 'web_search'


def desktop_title(messages):
    for message in messages:
        if message.get('role') != 'user':
            continue
        for block in blocks(message['content']):
            if block.get('type') != 'text':
                continue
            text = re.sub(r'<system-reminder>.*?</system-reminder>', '', block['text'], flags=re.S)
            title = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
            if title:
                return title if len(title) <= 64 else title[:63]+'…'
    return '附件对话'


def desktop_project():
    return Path.home()/'.local/share/safe-code/chats/Claude Desktop'


DESKTOP_WORKSPACE = (
    "SafeCode Desktop tool environment: The native CLI's Linux cwd is private chat storage. "
    "All forwarded filesystem and shell tools run on the client in the selected Desktop workspace. "
    "For a request about the current workspace, obtain the client cwd from the Desktop shell before "
    "the first filesystem action (on macOS use pwd, on Windows Git Bash use pwd -W, on PowerShell use Get-Location). "
    "Use the returned native absolute path for Read, Write and Edit. Never create or translate the "
    "server chat-storage path on the client. If the client cwd cannot be established, ask for it."
)


DESKTOP_FILE_TOOLS = {'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Grep', 'Glob'}
DESKTOP_PATH_FIELDS = ('file_path', 'notebook_path', 'path')


def desktop_tool_input(call, platform='windows'):
    # On Windows, Git Bash pwd prints /c/... while the file APIs take C:/...
    args = call.get('args', {}).copy()
    if platform == 'windows' and call['name'].rsplit('__', 1)[-1] in DESKTOP_FILE_TOOLS:
        for field in DESKTOP_PATH_FIELDS:
            value = args.get(field)
            if isinstance(value, str) and re.match(r'^/[a-zA-Z]/', value):
                args[field] = value[1].upper()+':'+value[2:]
    return args


def targets_chat_storage(call):
    """Check filesystem destinations, never the content the client is being asked to write."""
    name = call['name'].rsplit('__', 1)[-1]
    args = call.get('args', {})
    reserved = desktop_project().as_posix()
    if name == 'Bash':
        try:
            words = shlex.split(args.get('command', ''))
        except ValueError:
            return False
        if not words or words[0] not in ('cd', 'ls', 'mkdir', 'touch', 'rmdir', 'rm', 'cat', 'stat'):
            return False
        paths = words[1:]
    elif name in DESKTOP_FILE_TOOLS:
        paths = (args.get(field) for field in DESKTOP_PATH_FIELDS)
    else:
        return False
    for value in paths:
        if isinstance(value, str):
            normalized_path = os.path.normpath(value.replace(chr(92), '/'))
            if normalized_path == reserved or normalized_path.startswith(reserved+'/'):
                return True
    return False


# The existing bridge probes its configured binary with --help before starting a turn.
# Only Desktop installs this launcher; its daemon and MCP socket retain independent workspaces.
DESKTOP_LAUNCHER = """#!/bin/sh
cli="$HOME/.local/bin/claude"
if [ "$1" = "--help" ]; then
    help=$("$cli" "$@") || exit $?
    case "$help" in
        *"--name <"*) touch "$CODEX_CLAUDE_WIN_HOME/supports-name" ;;
        *) rm -f "$CODEX_CLAUDE_WIN_HOME/supports-name" ;;
    esac
    case "$help" in
        *"--system-prompt-snapshot"*) touch "$CODEX_CLAUDE_WIN_HOME/supports-prompt-refresh" ;;
        *) rm -f "$CODEX_CLAUDE_WIN_HOME/supports-prompt-refresh" ;;
    esac
    printf '%s\\n' "$help"
    exit 0
fi
if [ "$1" = "-p" ]; then
    cd "$SAFECODE_DESKTOP_PROJECT" || exit 1
    if [ -f "$CODEX_CLAUDE_WIN_HOME/supports-prompt-refresh" ]; then
        set -- "$@" --system-prompt-snapshot off
    fi
    if [ "$SAFECODE_DESKTOP_TRANSIENT" = "1" ]; then
        set -- "$@" --no-session-persistence
    elif [ -n "$SAFECODE_DESKTOP_TITLE" ] && [ -f "$CODEX_CLAUDE_WIN_HOME/supports-name" ]; then
        set -- "$@" "--name=$SAFECODE_DESKTOP_TITLE"
    fi
fi
unset SAFECODE_DESKTOP_PROJECT SAFECODE_DESKTOP_TRANSIENT SAFECODE_DESKTOP_TITLE
exec "$cli" "$@"
"""


def conversation_scope(data, header=None):
    # Only an explicit per-session UUID can narrow a history match.
    # A device/account-level metadata.user_id is shared across independent chats.
    metadata = data.get('metadata')
    value = metadata.get('user_id') if isinstance(metadata, dict) else None
    if isinstance(value, str) and value.startswith('{'):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    candidate = value.get('session_id') if isinstance(value, dict) else None
    try:
        body_id = str(uuid.UUID(candidate)) if isinstance(candidate, str) else None
        header_id = str(uuid.UUID(header)) if header else None
    except ValueError:
        raise GatewayError('Invalid Desktop session ID', 400)
    if body_id and header_id and body_id != header_id:
        raise GatewayError('Conflicting Desktop session IDs', 400)
    return body_id or header_id or ''


def desktop_suggestion(messages):
    # Desktop sends the suggested-reply request on the chat's own session ID,
    # after a real assistant reply. Only the final user turn identifies the helper.
    if len(messages) < 3 or messages[-2]['role'] != 'assistant':
        return False
    content = blocks(messages[-1]['content'])
    if not content or any(block.get('type') != 'text' for block in content):
        return False
    text = ''.join(block['text'] for block in content).strip()
    return (text.startswith('[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]')
            and text.endswith('Reply with ONLY the suggestion, no quotes or explanation.'))


def auxiliary_request(data, scope):
    if scope or data.get('tools') or len(data['messages']) != 1:
        return False
    content = blocks(data['messages'][0]['content'])
    if len(content) != 1 or content[0].get('type') != 'text':
        return False
    text = content[0]['text']
    if data['max_tokens'] == 1 and text in ('Hi', 'Hello') and not data.get('system'):
        return True
    # Claude Desktop sends a separate, sessionless request in this exact envelope.
    return (text.startswith('You are coming up with a succinct title and git branch name for a coding session ')
            and 'Here is the session description:\n<description>' in text
            and '</description>' in text)


def webfetch_summary_request(data):
    if data.get('tools') or len(data['messages']) != 1:
        return False
    content = blocks(data['messages'][0]['content'])
    if len(content) != 1 or content[0].get('type') != 'text':
        return False
    text = content[0].get('text')
    if not isinstance(text, str):
        return False
    text = text.strip()
    return (text.startswith('Web page content:\n---\n')
            and '\n---\n\n' in text
            and '\nProvide a concise response based only on the content above. In your response:\n' in text
            and text.endswith(' - Never produce or reproduce exact song lyrics.'))


def history_signature(messages, scope):
    if not scope:
        return signature(messages)
    # Desktop can refresh its context reminders or omit private thinking when it resends history.
    clean = []
    for msg in messages:
        visible = []
        for item in blocks(msg['content']):
            if item.get('type') in ('thinking', 'redacted_thinking'):
                continue
            if item.get('type') == 'text':
                value = re.sub(r'<system-reminder>.*?</system-reminder>', '', item['text'], flags=re.S)
                if not value.strip():
                    continue
                item['text'] = value
            visible.append(item)
        if visible:
            clean.append({'role':msg['role'], 'content':visible})
    return signature(clean)


class DesktopHistory:
    """One checkpoint per native chat. No user messages or desktop credentials are copied."""
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS turns (chat TEXT PRIMARY KEY, scope TEXT NOT NULL, '
                       'expected TEXT NOT NULL, tail TEXT NOT NULL, session TEXT NOT NULL, '
                       'request TEXT NOT NULL, reply TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS turns_scope ON turns(scope, expected)')

    def previous(self, scope, prefix, tail, request_id):
        with closing(sqlite3.connect(self.path)) as db:
            if scope:
                row = db.execute('SELECT reply FROM turns WHERE scope=? AND request=?',
                                 (scope, request_id)).fetchone()
                if row:
                    if row[0] is None:
                        raise GatewayError('This Desktop request has already completed', 409)
                    return None, json.loads(row[0])
            if not prefix:
                return None, None
            matches = db.execute('SELECT chat, session, expected FROM turns WHERE scope=? AND expected=? LIMIT 2',
                                 (scope, prefix)).fetchall()
            # A trimmed Desktop history can still prove continuity with the last complete exchange.
            if not matches and scope and tail:
                matches = db.execute('SELECT chat, session, expected FROM turns WHERE scope=? AND tail=? LIMIT 2',
                                     (scope, tail)).fetchall()
            return (matches[0] if len(matches) == 1 else None), None

    def consume(self, conversation):
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute('DELETE FROM turns WHERE chat=? AND expected=?',
                                 (conversation.client_id, conversation.checkpoint)).rowcount
            if not changed:
                raise GatewayError('Conversation history has already advanced', 409)

    def save(self, conversation, tail, request_id, response):
        reply = json.dumps(response, ensure_ascii=False) if conversation.scope else None
        if reply and len(reply.encode()) > MAX_REQUEST:
            reply = None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?)',
                       (conversation.client_id, conversation.scope, conversation.expected, tail,
                        conversation.session_id, request_id, reply))


class Channel:
    """复用现有协议；新 clientId 保证不会接管 Codex 的连接或取消其任务。"""
    def __init__(self, manager, data, directory=None, client_id=None):
        # Unix socket 路径有约 108 字节上限；客户端 UUID 的完整状态路径过长。
        self.directory = Path(directory) if directory else Path.home()/'.local/state/scd'/hashlib.sha256(
            str(manager.directory).encode()).hexdigest()[:12]/client_id.removeprefix('desktop_')[:20]
        if not self.directory.resolve().is_relative_to(Path.home().resolve()):raise GatewayError('Invalid CLI channel directory')
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.maximum = data['max_tokens']
        project = desktop_project()
        project.mkdir(mode=0o700, parents=True, exist_ok=True)
        launcher = self.directory/'desktop-cli'
        launcher.write_text(DESKTOP_LAUNCHER)
        launcher.chmod(0o700)
        (self.directory/'policy.json').write_text(json.dumps({'claudePath': str(launcher),
            'allowedNativeTools': ['none'], 'maxConcurrentTurns': 1, 'detachGraceMs': 5000, 'idleExitMs': 10000}))
        env = {k:v for k,v in os.environ.items() if not k.startswith(('SSH_', 'SSH2_', 'SUDO_'))}
        env.update(CODEX_CLAUDE_WIN_HOME=str(self.directory), CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(self.maximum),
                   SAFECODE_DESKTOP_PROJECT=str(project), SAFECODE_DESKTOP_TITLE=data['_desktop_title'] or '',
                   SAFECODE_DESKTOP_TRANSIENT='1' if data['_desktop_transient'] else '0')
        self.process = subprocess.Popen([manager.config['node'], str(Path(manager.config['library'])/'src/vps/connect.mjs')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=str(self.directory),
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        self.events = queue.Queue(maxsize=4096)
        self.lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self.reader, daemon=True).start()
        self.send({'op': 'hello', 'v': 1, 'role': 'client', 'clientId': client_id or 'desktop_'+uuid.uuid4().hex, 'resume': []})
        hello = self.next(25)
        if hello.get('op') != 'hello.ok':
            self.close()
            raise GatewayError('VPS CLI channel handshake failed')

    def send(self, header):
        encoded = json.dumps(header, ensure_ascii=False).encode()
        if len(encoded) > 16 * 1024 * 1024:
            raise GatewayError('CLI channel request too large', 413)
        with self.lock:
            try:
                self.process.stdin.write(struct.pack('>II', len(encoded), 0) + encoded)
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise GatewayError('VPS CLI channel disconnected')

    def reader(self):
        def read_n(n):
            result = bytearray()
            while len(result) < n:
                value = self.process.stdout.read(n-len(result))
                if not value:
                    raise EOFError()
                result.extend(value)
            return result
        try:
            while not self.closed:
                head, body = struct.unpack('>II', read_n(8))
                if head > 16 * 1024 * 1024 or body > 4 * 1024 * 1024:
                    raise ValueError()
                header = json.loads(read_n(head))
                read_n(body)
                self.events.put(header, timeout=2)
        except (OSError, EOFError, ValueError, queue.Full):
            try:self.events.put_nowait({'op': 'channel.error'})
            except queue.Full:pass

    def next(self, timeout=180):
        try:event = self.events.get(timeout=timeout)
        except queue.Empty:raise GatewayError('VPS Claude Code timed out', 504)
        if event.get('op') == 'channel.error':
            raise GatewayError('VPS CLI channel disconnected')
        return event

    def close(self):
        self.closed = True
        try:self.process.stdin.close()
        except (OSError, ValueError):pass
        try:self.process.wait(2)
        except subprocess.TimeoutExpired:self.process.terminate()
        # 仅关闭本通道自己的守护进程；Codex 的共享 daemon 完全独立。
        pidfile = self.directory/'run/daemon.pid'
        try:
            pid = int(pidfile.read_text())
            args = Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
            if any(str(self.directory/'run/daemon.sock').encode() == arg for arg in args):
                os.killpg(pid, signal.SIGTERM)
                deadline=time.monotonic()+2
                while pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
        except (OSError, ValueError):pass


class Conversation:
    def __init__(self, manager, slots):
        self.manager = manager
        self.slots = slots
        self.channel = None
        self.channel_dir = None
        self.client_id = 'desktop_'+uuid.uuid4().hex
        self.scope = ''
        self.transient = False
        self.checkpoint = None
        self.incoming = None
        self.handling = False
        self.has_slot = False
        self.lock = threading.Lock()
        self.session_id = None
        self.turn_id = None
        self.pending = {}
        self.expected = None
        self.last_used = time.monotonic()
        self.turn_count = 0

    def close(self):
        if self.turn_id and self.channel:
            try:self.channel.send({'op': 'turn.cancel', 'turnId': self.turn_id, 'reason': 'desktop-gateway-stop'})
            except GatewayError:pass
        if self.channel:self.channel.close();self.channel=None
        if self.has_slot:self.slots.release();self.has_slot=False

    def run(self, data):
        if not self.lock.acquire(blocking=False):
            raise GatewayError('This conversation already has an active request', 409)
        try:
            self.last_used = time.monotonic()
            messages = data['messages']
            if self.pending:
                if data['max_tokens']!=self.channel.maximum or data['model']!=self.turn_model:
                    raise GatewayError('Model/output budget cannot change while a CLI tool call is pending',409)
                supplied = {b.get('tool_use_id'): b for b in blocks(messages[-1]['content']) if b.get('type') == 'tool_result'}
                if set(supplied) != set(self.pending):
                    raise GatewayError('All pending tool results are required', 400)
                for external_id, call in self.pending.items():
                    result = supplied[external_id]
                    self.channel.send({'op': 'turn.toolresult', 'turnId': self.turn_id, 'callId': call['callId'],
                                       'content': blocks(result.get('content', '')), 'isError': bool(result.get('is_error'))})
                self.pending = {}
            else:
                content = blocks(messages[-1]['content'])
                if any(b.get('type') == 'tool_result' for b in content):
                    raise GatewayError('Tool session expired; start a new conversation', 409)
                if not self.slots.acquire(blocking=False):raise GatewayError('Native CLI turn capacity is busy',429)
                self.has_slot=True
                channel_data = {**data, '_desktop_title': None if self.session_id else desktop_title(messages),
                                '_desktop_transient': self.transient}
                self.channel=Channel(self.manager,channel_data,self.channel_dir,self.client_id)
                self.channel_dir=self.channel.directory
                self.turn_model=data['model']
                if not self.session_id and len(messages) > 1:
                    # 迁移旧会话时保留角色结构；不会伪造 CLI transcript 或登录状态。
                    content.insert(0, {'type': 'text', 'text': 'Previous conversation (JSON, preserve roles and tool results):\n'+json.dumps(messages[:-1], ensure_ascii=False)})
                system = '\n'.join(b.get('text', '') for b in blocks(data.get('system', '')) if b.get('type') == 'text')
                if data.get('thinking', {}).get('type') == 'disabled':
                    raise GatewayError('The native CLI cannot guarantee per-request thinking=disabled', 400)
                tools = [] if data.get('tool_choice', {}).get('type') == 'none' else data.get('tools', [])
                if tools:
                    system = (system+'\n\n'+DESKTOP_WORKSPACE).strip()
                spec = {'model': data['model'], 'workspaceKey': hashlib.sha256(str(self.manager.directory).encode()).hexdigest()[:32],
                        'nativeTools': 'none', 'permissionMode': 'dontAsk', 'systemPrompt': system,
                        'tools': [{'name': t['name'], 'description': t.get('description', ''), 'inputSchema': t['input_schema']} for t in tools],
                        'userMessage': {'type': 'user', 'message': {'role': 'user', 'content': content}}, 'disallowAskUserQuestion': True}
                if self.session_id:spec['resume'] = self.session_id
                if data.get('output_config', {}).get('effort'):spec['effort'] = data['output_config']['effort']
                self.turn_id = 'desktop_'+uuid.uuid4().hex
                if self.session_id:
                    self.manager.history.consume(self)
                self.channel.send({'op': 'turn.start', 'turnId': self.turn_id, 'spec': spec})
                self.turn_count += 1
                self.manager.started += 1
            content, usage = [], {}
            message_id=None
            usage_model=None
            stream_message=StreamMessage();native_error=None;truncated=False
            calls = {}
            tool_batch_deadline = None
            deadline = time.monotonic() + 600
            while True:
                remaining = min(deadline, tool_batch_deadline or deadline) - time.monotonic()
                try:
                    if remaining <= 0:
                        raise GatewayError('VPS Claude Code timed out', 504)
                    event = self.channel.next(remaining)
                except GatewayError as error:
                    if error.status == 504 and tool_batch_deadline is not None:
                        raise GatewayError('VPS Claude Code did not deliver the complete tool call batch', 502) from error
                    raise
                if event.get('turnId') != self.turn_id:continue
                op = event.get('op')
                if op == 'turn.error':raise GatewayError('VPS Claude Code refused this request: '+str(event.get('code', 'unknown')))
                if op == 'turn.event':
                    try:line = json.loads(event['line'])
                    except (KeyError, ValueError):continue
                    if line.get('session_id'):self.session_id = line['session_id']
                    if line.get('type') == 'stream_event':
                        stream=line.get('event',{})
                        stream_message.feed(stream)
                        if stream.get('type')=='message_start':
                            message_id=stream.get('message',{}).get('id')
                            usage_model=stream.get('message',{}).get('model')
                            usage=stream.get('message',{}).get('usage',{}).copy()
                        elif stream.get('type')=='message_delta':
                            for key,value in stream.get('usage',{}).items():
                                if isinstance(value,int):usage[key]=max(usage.get(key,0),value)
                                elif key=='cache_creation' and isinstance(value,dict):usage[key]=value
                        if message_id:self.manager.count_usage({'id':message_id,'usage':usage,'model':usage_model},self.turn_id)
                    elif line.get('type') == 'assistant':
                        if line.get('isApiErrorMessage') or line.get('error'):
                            native_error=line.get('error');continue
                        message = line.get('message', {})
                        content = message.get('content', [])
                        if message_id!=message.get('id'):usage={};message_id=message.get('id')
                        for key,value in message.get('usage',{}).items():
                            if isinstance(value,int):usage[key]=max(usage.get(key,0),value)
                            elif key=='cache_creation' and isinstance(value,dict):usage[key]=value
                        self.manager.count_usage({'id':message_id,'usage':usage,'model':message.get('model')},self.turn_id)
                    elif line.get('type') == 'result':
                        self.manager.record_result(self.turn_id,line.get('usage',{}))
                        if line.get('is_error'):
                            if native_error=='max_output_tokens' or (native_error is None and line.get('api_error_status') is None and (stream_message.stop=='max_tokens' or line.get('stop_reason')=='max_tokens')):
                                truncated=True;content=stream_message.partial()
                            else:raise GatewayError('VPS Claude Code could not complete the model request ('+str(line.get('subtype','unknown'))+')')
                elif op == 'turn.toolcall':
                    calls[event['callId']] = event
                elif op == 'turn.exit':
                    if (event.get('code') != 0 or event.get('spawnError')) and not truncated:
                        raise GatewayError('VPS Claude Code exited unsuccessfully')
                    if not truncated and (calls or stream_message.stop == 'tool_use'):
                        raise GatewayError('VPS Claude Code exited before delivering the complete tool call batch', 502)
                    self.turn_id = None
                    stop = 'max_tokens' if truncated else 'end_turn'
                    if not truncated:content=stream_message.partial() or content
                    self.channel.close();self.channel=None
                    if self.has_slot:self.slots.release();self.has_slot=False
                    if truncated:self.session_id=None
                    break
                # Assistant frames can each contain only one block of the same message.
                # The completed Claude stream supplies the full tool-use set independently
                # of the order in which the MCP calls arrive.
                streamed_tools = stream_message.stop == 'tool_use'
                if (calls or streamed_tools) and tool_batch_deadline is None:
                    tool_batch_deadline = time.monotonic() + 15
                if not calls:
                    continue
                legacy_batch = not stream_message.started and not any(call.get('toolUseId') for call in calls.values())
                if not streamed_tools and not legacy_batch:
                    continue
                source = stream_message.partial() if streamed_tools else content
                tool_blocks = stream_message.content.values() if streamed_tools else source
                expected = [block['id'] for block in tool_blocks if block.get('type') == 'tool_use']
                if not expected or len(calls) < len(expected):
                    continue
                by_native_id = {call['toolUseId']: call for call in calls.values() if call.get('toolUseId')}
                if by_native_id and all(identity in by_native_id for identity in expected):
                    batch = [by_native_id[identity] for identity in expected]
                elif not by_native_id and len(calls) == len(expected):
                    batch = list(calls.values())
                else:
                    continue
                content = [block for block in source if block.get('type') != 'tool_use']
                for call in batch:
                    external_id = call['callId']
                    if targets_chat_storage(call):
                        self.channel.send({'op': 'turn.toolresult', 'turnId': self.turn_id, 'callId': external_id,
                                           'isError': True, 'content': [{'type': 'text', 'text': DESKTOP_WORKSPACE}]})
                        continue
                    self.pending[external_id] = call
                    content.append({'type': 'tool_use', 'id': external_id, 'name': call['name'],
                                    'input': desktop_tool_input(call, self.manager.config.get('clientPlatform', 'windows'))})
                if self.pending:
                    stop = 'tool_use'
                    break
                content = []
                calls.clear()
                tool_batch_deadline = None
                stream_message = StreamMessage()
            if not content and stop!='max_tokens':raise GatewayError('VPS Claude Code returned no assistant content')
            response = {'id': 'msg_'+uuid.uuid4().hex, 'type': 'message', 'role': 'assistant', 'model': data['model'],
                        'content': content, 'stop_reason': stop, 'stop_sequence': None,
                        'usage': {**usage, **{k: int(usage.get(k, 0)) for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')}}}
            self.expected = history_signature(messages+[{'role': 'assistant', 'content': content}], self.scope)
            self.last_used = time.monotonic()
            return response
        except Exception:
            if self.channel or self.has_slot:
                self.close();self.pending.clear();self.expected = None
            raise
        finally:self.lock.release()


class Manager:
    def __init__(self, directory, config):
        self.directory, self.config = Path(directory), config
        self.conversations = []
        self.history = DesktopHistory(self.directory/'desktop-history.sqlite3')
        self.lock = threading.Lock()
        self.last_lease = time.monotonic()
        self.started = 0
        self.usage = {}
        self.turn_messages = {}
        self.completed_usage = {}
        self.capacity = threading.BoundedSemaphore(4)
        self.live_slots = threading.BoundedSemaphore(2)
        self.webfetch_slots = threading.BoundedSemaphore(1)
        self.models_lock = threading.Lock()
        self.models = None
        self.classifier_test = classifier_test_route()

    def catalog(self):
        with self.models_lock:
            if self.models is None:
                self.models = native_model_catalog()
                if self.classifier_test:
                    for model in self.models['data']:
                        if model['id']==self.classifier_test['model']:model['auto_mode_classifier']=True
            return self.models

    def count_usage(self, message, turn_id='unbound'):
        identity = message.get('id')
        if not identity:return
        with self.lock:
            self.turn_messages.setdefault(turn_id,set()).add(identity)
            previous = self.usage.setdefault(identity, {})
            for key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
                value = message.get('usage', {}).get(key, 0)
                if isinstance(value, int) and value >= 0:previous[key] = max(previous.get(key, 0), value)

    def record_result(self,turn_id,usage):
        if not isinstance(usage,dict) or not usage:return
        with self.lock:
            self.completed_usage[turn_id]={k:v for k,v in usage.items() if k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens') and isinstance(v,int) and v>=0}

    def status(self):
        self.last_lease = time.monotonic()
        with self.lock:
            usage={k:0 for k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens')}
            for turn_id in self.turn_messages.keys()|self.completed_usage.keys():
                parts=[self.completed_usage[turn_id]] if turn_id in self.completed_usage else [self.usage[i] for i in self.turn_messages[turn_id]]
                for key in usage:usage[key]+=sum(v.get(key,0) for v in parts)
        return {'ok': True, 'bridge': 'vps-claude-code', 'instance': self.config['instance'], 'pid': os.getpid(),
                'turnsStarted': self.started, 'usageAvailable': bool(self.usage or self.completed_usage), 'usage': usage}

    def finish_conversation_request(self, conversation):
        """Finalize request accounting before this conversation accepts another request."""

    def pending_tool(self, scope, name):
        if not scope:
            return None
        return next((
            c for c in self.conversations
            if c.scope == scope and not c.handling and not c.transient
            and any(call['name'].rsplit('__', 1)[-1] == name for call in c.pending.values())
        ), None)

    def run_test_web_search(self, data, session_header):
        tool = data['tools'][0]
        if tool.keys() & {'allowed_domains', 'blocked_domains', 'user_location'}:
            raise GatewayError('The test provider cannot enforce WebSearch domain or location restrictions', 400)
        uses = tool.get('max_uses')
        if set(tool) != {'type', 'name', 'max_uses'} or type(uses) is not int or not 1 <= uses <= 8:
            raise GatewayError('Unsupported native WebSearch tool options', 400)
        model = data.get('model')
        if not isinstance(model, str) or not model.startswith('claude-') or not MODEL.fullmatch(model):
            raise GatewayError('A native Claude model identifier is required', 400)
        maximum = data.get('max_tokens')
        if type(maximum) is not int or not 1 <= maximum <= 128000:
            raise GatewayError('max_tokens must be between 1 and 128000', 400)
        messages = data.get('messages')
        if (not isinstance(messages, list) or not messages or not all(
                isinstance(message, dict) and message.get('role') in ('user', 'assistant') for message in messages)
                or messages[0]['role'] != 'user'):
            raise GatewayError('Invalid web search messages', 400)
        scope = conversation_scope(data, session_header)
        with self.lock:
            parent = self.pending_tool(scope, 'WebSearch')
            if parent is None:
                raise GatewayError('No WebSearch tool call is pending in this conversation', 409)
            parent.last_used = time.monotonic()
        response = test_messages_request(self.classifier_test, data)
        if (response.get('stop_reason') not in ('end_turn', 'max_tokens', 'pause_turn', 'stop_sequence', 'refusal')
                or not isinstance(response.get('usage'), dict)
                or not isinstance(response.get('content'), list)
                or any(block.get('type') == 'tool_use' for block in response['content'])):
            raise GatewayError('Invalid local web search response', 502)
        turn = 'websearch_' + uuid.uuid4().hex
        self.count_usage(response, turn)
        self.record_result(turn, response['usage'])
        return response

    def count_input_tokens(self, data, headers=None):
        if not self.classifier_test:
            raise GatewayError('Token counting is unavailable on this CLI bridge', 501)
        model = data.get('model') if isinstance(data, dict) else None
        if not isinstance(model, str) or not model.startswith('claude-') or not MODEL.fullmatch(model):
            raise GatewayError('A native Claude model identifier is required', 400)
        messages = data.get('messages')
        if not isinstance(messages, list) or not messages or 'stream' in data:
            raise GatewayError('Invalid token counting request', 400)
        reply = simulator_request(self.classifier_test, COUNT_TOKENS_PATH, data, headers=headers)
        tokens = reply.get('input_tokens') if isinstance(reply, dict) else None
        if type(tokens) is not int or tokens < 0:
            raise GatewayError('Invalid local token counting response', 502)
        return {'input_tokens': tokens}

    def run(self, data, context_1m=False, session_header=None):
        requested_model = data.get('model') if isinstance(data, dict) else None
        if self.classifier_test and native_web_search_request(data):
            return self.run_test_web_search(data, session_header)
        if self.classifier_test and (requested_model == self.classifier_test['model'] or desktop_auto_check(data)):
            if not data.get('tools'):
                response = run_test_classifier(self.classifier_test,data)
                self.started += 1
                self.count_usage(response,'classifier_'+uuid.uuid4().hex)
                return response
            # Older Desktop profiles offered the test-only classifier in the chat picker.
            # For an existing tool-bearing chat, use the native CLI's regular Sonnet alias.
            data = {**data, 'model':'sonnet'}
        data=validate_request(data)
        if context_1m and not data['model'].endswith('[1m]'):
            data = {**data, 'model': data['model']+'[1m]'}
        scope = conversation_scope(data, session_header)
        prefix = history_signature(data['messages'][:-1], scope)
        results = {b.get('tool_use_id') for b in blocks(data['messages'][-1]['content']) if b.get('type') == 'tool_result'}
        messages = data['messages']
        suggestion = desktop_suggestion(messages)
        transient = suggestion or auxiliary_request(data, scope)
        webfetch_summary = bool(scope) and webfetch_summary_request(data)
        if suggestion:
            data = {**data, 'tools': []}
        tail = history_signature(messages[-3:-1], scope) if len(messages) == 3 else None
        request_id = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if scope else ''

        with self.lock:
            if results:
                candidates = [c for c in self.conversations if c.scope == scope and results == set(c.pending)]
                if len(candidates) != 1:
                    raise GatewayError('Tool session expired; restart this conversation', 409)
                conversation = candidates[0]
                if conversation.handling:
                    raise GatewayError('This conversation already has an active request', 409)
            else:
                # WebFetch summarizes the page before returning its tool result.
                # Tie the helper to a pending WebFetch in this same client session.
                webfetch_parent = self.pending_tool(scope, 'WebFetch') if webfetch_summary else None
                if webfetch_parent:
                    webfetch_parent.last_used = time.monotonic()
                    transient = True
                if not transient and any(not c.transient and c.scope == scope and (scope or (len(messages) > 1 and c.incoming == prefix))
                                         for c in self.conversations):
                    raise GatewayError('This conversation already has an active request', 409)
                conversation = Conversation(self, self.webfetch_slots if webfetch_parent else self.live_slots)
                conversation.scope = scope
                conversation.transient = transient
                conversation.incoming = prefix
                if not transient:
                    previous, cached = self.history.previous(scope, prefix if len(messages) > 1 else None, tail, request_id)
                    if cached is not None:
                        return cached
                    if previous:
                        conversation.client_id, conversation.session_id, conversation.checkpoint = previous
                self.conversations.append(conversation)
            conversation.handling = True
        try:
            response = conversation.run(data)
            response['model'] = requested_model
            if not conversation.pending and conversation.session_id and not transient:
                last_exchange = [messages[-1], {'role':'assistant', 'content':response['content']}]
                self.history.save(conversation, history_signature(last_exchange, scope), request_id, response)
        finally:
            with self.lock:
                try:
                    self.finish_conversation_request(conversation)
                finally:
                    conversation.handling = False
                    if not conversation.pending:
                        self.conversations.remove(conversation)
        return response

    def close(self):
        for c in list(self.conversations):c.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, manager):
        self.manager = manager
        super().__init__(('127.0.0.1', manager.config.get('listenPort', 0)), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):pass

    @property
    def manager(self):
        return self.server.manager

    def authorized(self):
        token = self.headers.get('Authorization', '')
        token = token[7:] if token.startswith('Bearer ') else self.headers.get('x-api-key', '')
        return hmac.compare_digest(token.encode(), self.manager.config['token'].encode())

    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)));self.end_headers();self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():return self.reply(401, {'error': {'type': 'authentication_error', 'message': 'Local bridge token required'}})
        if self.path == '/health':return self.reply(200, self.manager.status())
        if self.path.split('?')[0] == '/v1/models':
            try:return self.reply(200, self.manager.catalog())
            except (GatewayError, OSError, ValueError, TypeError, AttributeError):
                return self.reply(503, {'error': {'type': 'api_error', 'message': 'Native Claude Code model discovery unavailable'}})
        self.reply(404, {'error': {'type': 'not_found_error', 'message': 'Unknown CLI bridge endpoint'}})

    def execute_message(self, data, *, count_headers=None, **options):
        if not self.manager.capacity.acquire(blocking=False):
            raise GatewayError('Too many active Desktop requests', 429)
        try:
            if count_headers is not None:
                return self.manager.count_input_tokens(data, count_headers)
            return self.manager.run(data, **options)
        finally:
            self.manager.capacity.release()

    def do_POST(self):
        self.close_connection = True
        if not self.authorized():return self.reply(401, {'error': {'type': 'authentication_error', 'message': 'Local bridge token required'}})
        if self.path == '/shutdown':
            self.reply(200, {'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        path = self.path.partition('?')[0]
        if path not in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            return self.reply(501, {'error': {'type': 'api_error', 'message': 'This endpoint is not supported by the VPS CLI bridge'}})
        try:
            if self.headers.get('Transfer-Encoding'):raise GatewayError('Content-Length required', 400)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:raise GatewayError('Request too large or empty', 413)
            self.connection.settimeout(30)
            data = json.loads(self.rfile.read(length))
            if path == COUNT_TOKENS_PATH:
                headers = {name: self.headers[name] for name in ('anthropic-version', 'anthropic-beta')
                           if name in self.headers}
                return self.reply(200, self.execute_message(data, count_headers=headers))
            response = self.execute_message(data, session_header=self.headers.get('x-claude-code-session-id'),
                context_1m='context-1m-2025-08-07' in {v.strip() for v in self.headers.get('anthropic-beta', '').split(',')})
            if data.get('stream'):
                self.send_response(200);self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache');self.end_headers()
                for event, payload in sse_response(response):
                    self.wfile.write(('event: '+event+'\ndata: '+json.dumps(payload, ensure_ascii=False)+'\n\n').encode())
                    self.wfile.flush()
            else:self.reply(200, response)
        except GatewayError as error:
            self.reply(error.status, {'type': 'error', 'error': {'type': 'api_error', 'message': str(error)}})
        except (ValueError, TypeError, KeyError, AttributeError):
            self.reply(400, {'error': {'type': 'invalid_request_error', 'message': 'Invalid Messages request'}})
        except sqlite3.Error:
            self.reply(503, {'error': {'type':'api_error', 'message':'Desktop conversation history unavailable'}})
        except (OSError, BrokenPipeError):pass


def sse_response(response):
    start = {**response, 'content': [], 'stop_reason': None}
    start['usage'] = {**response['usage'], 'output_tokens': 0}
    yield 'message_start', {'type': 'message_start', 'message': start}
    for index, block in enumerate(response['content']):
        kind=block.get('type')
        initial=copy.deepcopy(block)
        if kind=='text':initial['text']=''
        elif kind=='thinking':initial.update(thinking='',signature='')
        elif kind in ('tool_use','server_tool_use'):initial['input']={}
        yield 'content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': initial}
        if kind=='text':
            yield 'content_block_delta', {'type':'content_block_delta','index':index,'delta':{'type':'text_delta','text':block.get('text','')}}
        elif kind=='thinking':
            yield 'content_block_delta', {'type':'content_block_delta','index':index,'delta':{'type':'thinking_delta','thinking':block.get('thinking','')}}
            if block.get('signature'):
                yield 'content_block_delta', {'type':'content_block_delta','index':index,'delta':{'type':'signature_delta','signature':block['signature']}}
        elif kind in ('tool_use','server_tool_use'):
            yield 'content_block_delta', {'type':'content_block_delta','index':index,'delta':{'type':'input_json_delta','partial_json':json.dumps(block.get('input',{}),ensure_ascii=False)}}
        yield 'content_block_stop', {'type': 'content_block_stop', 'index': index}
    yield 'message_delta', {'type': 'message_delta', 'delta': {'stop_reason': response['stop_reason'], 'stop_sequence': response.get('stop_sequence')}, 'usage': response['usage']}
    yield 'message_stop', {'type': 'message_stop'}


def serve(directory):
    directory = Path(directory)
    config = json.loads((directory/'config.json').read_text())
    manager = Manager(directory, config)
    server = Server(manager)
    (directory/'running.json').write_text(json.dumps({'managedBy': 'safe-code-desktop-vps', 'pid': os.getpid(), 'port': server.server_port, 'instance': config['instance']}))
    def shutdown(*args):threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown);signal.signal(signal.SIGINT, shutdown)
    def lease():
        while True:
            time.sleep(10)
            if time.monotonic()-manager.last_lease > 90:
                shutdown();return
    threading.Thread(target=lease, daemon=True).start()
    try:server.serve_forever()
    finally:
        manager.close();server.server_close();(directory/'running.json').unlink(missing_ok=True)


def manage(directory, request):
    """控制请求只通过本机回环 HTTP，不接触模型 API 或 Claude 登录凭据。"""
    import urllib.request
    directory = Path(directory)
    marker = directory/'component.json'
    if directory.is_symlink() or (directory.exists() and any(directory.iterdir()) and not marker.is_file()):
        raise GatewayError('Unknown gateway directory; refusing to overwrite')
    if marker.exists() and (marker.is_symlink() or json.loads(marker.read_text()).get('managedBy') != 'safe-code-desktop-vps'):
        raise GatewayError('Unknown gateway owner')
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name in ('config.json', 'running.json'):
        if (directory/name).is_symlink():raise GatewayError('Gateway state must not be a symlink')
    marker.write_text(json.dumps({'managedBy': 'safe-code-desktop-vps'}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    running = directory/'running.json'
    configpath = directory/'config.json'
    if request['action']=='stop' and configpath.exists() and json.loads(configpath.read_text()).get('instance')!=request.get('instance'):
        return {'ready':False,'port':0,'instance':'','warning':None}
    if running.exists():
        state = json.loads(running.read_text())
        if state.get('managedBy') != 'safe-code-desktop-vps':raise GatewayError('Unknown gateway state')
        config = json.loads(configpath.read_text())
        headers = {'Authorization': 'Bearer '+config['token']}
        try:
            with opener.open(urllib.request.Request('http://127.0.0.1:'+str(state['port'])+'/health', headers=headers), timeout=3) as response:
                health = json.load(response)
            if health.get('instance') != config['instance']:raise GatewayError('Gateway instance changed')
            with opener.open(urllib.request.Request('http://127.0.0.1:'+str(state['port'])+'/shutdown', data=b'', headers=headers), timeout=3):pass
            for _ in range(100):
                if not running.exists():break
                time.sleep(.1)
            if running.exists():raise GatewayError('Previous gateway is still stopping')
        except (OSError, ValueError):
            # 从未向不确定身份的 PID 发信号；过期监听文件本身不等于可杀的进程。
            if Path('/proc/'+str(state.get('pid', -1))).exists():raise GatewayError('Gateway stop could not be verified')
            running.unlink(missing_ok=True)
    if request['action'] == 'stop':
        configpath.unlink(missing_ok=True)
        return {'ready': False, 'port': 0, 'instance': '', 'warning': None}
    if request['action'] != 'start':raise GatewayError('Unknown gateway action', 400)
    import zipfile, io
    archive = base64.b64decode(request['bridgeSource'])
    if hashlib.sha256(archive).hexdigest() != request['bridgeHash']:raise GatewayError('Remote component checksum mismatch')
    library = Path.home()/'.local/share/safe-code/desktop-vps-components'/request['bridgeHash'][:16]
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for name in source.namelist():
            relative = Path(name)
            destination = library/relative
            if relative.is_absolute() or '..' in relative.parts or not destination.resolve().is_relative_to(Path.home().resolve()):raise GatewayError('Invalid component path')
            content = source.read(name)
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != content:raise GatewayError('Existing component was modified; refusing to overwrite')
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(content)
    config = {'token': request['token'], 'instance': request['instance'], 'library': str(library), 'node': request['node']}
    configpath.write_text(json.dumps(config));configpath.chmod(0o600)
    subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--serve', str(directory)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(directory), start_new_session=True)
    for _ in range(100):
        if running.exists():
            state = json.loads(running.read_text())
            if state.get('instance') == config['instance']:
                return {'ready': True, 'port': state['port'], 'instance': config['instance'], 'warning': None}
        time.sleep(.1)
    raise GatewayError('VPS gateway did not start')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser();group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--serve');group.add_argument('--manage')
    args=parser.parse_args()
    if args.serve:serve(args.serve)
    else:print(json.dumps(manage(args.manage, json.load(sys.stdin))))
