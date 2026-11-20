#!/usr/bin/env python3
"""Claude Desktop Messages API → 现有 VPS 桥接守护进程 → 原版 Claude Code。

模型请求由远端 claude 进程发出。桥接服务不读取上游模型密钥。
状态、会话及正文只在工作用户范围内；HTTP 仅回环监听，需独立随机 token。
"""
import argparse
import base64
import copy
from contextlib import contextmanager, closing
from contextvars import ContextVar
import fcntl
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import re
import select
import sqlite3
import signal
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
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


REQUEST_DIAGNOSTIC = ContextVar('request_diagnostic', default=None)
DIAGNOSTIC_HEADER = 'X-SafeCode-Gateway-Diagnostic'
REJECTION_TYPES = frozenset((
    'invalid_request_error', 'authentication_error', 'billing_error', 'permission_error',
    'not_found_error', 'request_too_large', 'rate_limit_error', 'api_error', 'overloaded_error',
))
PROVIDER_ID = re.compile(r'req_[a-zA-Z0-9]{8,80}')


def diagnostic(**values):
    trace = REQUEST_DIAGNOSTIC.get()
    if trace is not None:
        trace.update(values)


def anthropic_error(event, detail=None):
    """Extract only Anthropic's fixed error fields from a CLI API-error event."""
    event_error = event.get('error')
    nodes = [item for item in (event, event_error, detail) if isinstance(item, dict)]
    status = next((item['api_error_status'] for item in nodes
                   if type(item.get('api_error_status')) is int and 400 <= item['api_error_status'] <= 599), None)
    evidence = 'cli_event'
    prefix = re.match(r'^(?:API Error:|API error)\s*([45]\d\d)\b', detail) if isinstance(detail, str) else None
    if prefix:
        if status is None:
            evidence = 'cli_error_text'
        status = int(prefix[1])
        remainder = detail[prefix.end():].lstrip(' -:')
        if remainder.startswith('{') and len(remainder) <= 16384:
            try:
                parsed = json.loads(remainder)
                if isinstance(parsed, dict):
                    nodes.append(parsed)
            except ValueError:
                pass
    for item in tuple(nodes):
        if isinstance(item.get('error'), dict):
            nodes.append(item['error'])
    kind = next((item['type'] for item in nodes if isinstance(item.get('type'), str) and item['type'] in REJECTION_TYPES), None)
    request = next((item['request_id'] for item in nodes if isinstance(item.get('request_id'), str)
                    and PROVIDER_ID.fullmatch(item['request_id'])), None)
    if status is not None or request is not None:
        diagnostic(stage='anthropic_response', anthropicEvidence=evidence, anthropicStatus=status,
                   anthropicError=kind, anthropicRequestId=request)
    return status


class GatewayError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


CLOUDCLI_CONFIG = Path('.config/safe-code/cloudcli-gateway.json')
CLOUDCLI_MCP_SCRIPT = '/opt/safe-code/desktop-api/host_control_mcp.py'
CLOUDCLI_SESSION_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}')
CLOUDCLI_ERROR_STATUS = {400, 401, 402, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504, 529}
CLOUDCLI_USAGE_FIELDS = {
    'input_tokens': 'inputTokens',
    'output_tokens': 'outputTokens',
    'cache_creation_input_tokens': 'cacheCreationTokens',
    'cache_read_input_tokens': 'cacheReadTokens',
}


def load_cloudcli_config(home=None):
    """Read the work account's opt-in configuration, without probing either provider."""
    path = (Path.home() if home is None else Path(home))/CLOUDCLI_CONFIG
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError()
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        return cloudcli_config(json.loads(raw))
    except (OSError, ValueError, TypeError, OverflowError):
        raise GatewayError('CloudCLI configuration is invalid or is not private to the work account', 503) from None


def cloudcli_config(value):
    if not isinstance(value, dict):
        raise ValueError()
    port = value.get('port', 3001)
    api_key = value.get('apiKey')
    project = value.get('projectPath')
    models = value.get('models')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError()
    if not isinstance(api_key, str) or not re.fullmatch(r'[!-~]{16,512}', api_key):
        raise ValueError()
    if not isinstance(project, str) or not Path(project).is_absolute() or not Path(project).is_dir():
        raise ValueError()
    if not isinstance(models, list) or not 1 <= len(models) <= 32 or any(
        not isinstance(model, str) or len(model) > 80 or not model.startswith('claude-')
        or not MODEL.fullmatch(model) or model.endswith('[1m]') for model in models
    ) or len(set(models)) != len(models):
        raise ValueError()
    host = value.get('hostControl')
    if host is not None:
        if not isinstance(host, dict):
            raise ValueError()
        client = host.get('clientId')
        workspace = host.get('workspace')
        name = host.get('deviceName', '')
        if not isinstance(client, str) or not re.fullmatch(r'[0-9a-f]{20}', client):
            raise ValueError()
        if not valid_mac_workspace(workspace):
            raise ValueError()
        if not isinstance(name, str) or len(name) > 100 or any(ord(c) < 32 for c in name + workspace):
            raise ValueError()
        roots = host.get('projectWorkspaces')
        allow_ssh_shell = host.get('allowSshShell', False)
        if type(allow_ssh_shell) is not bool:
            raise ValueError()
        host = {'clientId': client, 'workspace': workspace, 'deviceName': name}
        if roots is not None:
            if (not isinstance(roots, list) or not 1 <= len(roots) <= 16
                or not all(valid_mac_workspace(root) for root in roots) or len(set(roots)) != len(roots)):
                raise ValueError()
            host['projectWorkspaces'] = roots
        if allow_ssh_shell:
            host['allowSshShell'] = True
    return {'port': port, 'apiKey': api_key, 'projectPath': str(Path(project).resolve()), 'models': models,
            'hostControl': host}


def valid_mac_workspace(value):
    return (isinstance(value, str) and value.startswith('/') and len(value.encode()) <= 4096
            and all(part not in ('', '.', '..') for part in value[1:].split('/'))
            and not re.search(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]', value))


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


def desktop_working_directory(data):
    """Extract the native Environment hint for matching an authorized project."""
    system = data.get('system') if isinstance(data, dict) else None
    if isinstance(system, str):
        prompt = system
    elif isinstance(system, list) and all(isinstance(block, dict) and block.get('type') == 'text'
                                          and isinstance(block.get('text'), str) for block in system):
        prompt = '\n'.join(block['text'] for block in system)
    else:
        return None

    sections = list(re.finditer(r'^# Environment[ \t]*\r?\n'
                                r'You have been invoked in the following environment:[ \t]*\r?\n'
                                r'((?:[ \t]*- [^\r\n]*(?:\r?\n|$))*)', prompt, re.M))
    if len(sections) != 1:
        return None

    fields = re.findall(r'^[ \t]*- Primary working directory\b([^\r\n]*)', sections[0][1], re.M)
    if len(fields) != 1 or not fields[0].startswith(': '):
        return None
    value = fields[0][2:]
    if len(value) > 4096 or re.search(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]', value):
        return None
    if re.match(r'^[A-Za-z]:[/\\]', value):
        path = PureWindowsPath(value)
        if any(part.endswith(('.', ' ')) or re.search(r'[<>:"|?*]', part)
               or PureWindowsPath(part).is_reserved() for part in path.parts[1:]):
            return None
    elif value.startswith('/') and not value.startswith('//') and '\\' not in value:
        path = PurePosixPath(value)
    else:
        return None
    return path if path.is_absolute() and path.name and '..' not in path.parts else None


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


class CloudCliError(GatewayError):
    """A safe error at the CloudCLI boundary; never exposes agent text or paths."""
    def __init__(self, message, status=502):
        super().__init__(message, status)
        self.error_type = {
            401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error',
            413: 'request_too_large', 429: 'rate_limit_error', 529: 'overloaded_error',
        }.get(status, 'invalid_request_error' if 400 <= status < 500 else 'api_error')


def cloudcli_failure(event):
    detail = event.get('content') or event.get('error') or event.get('message')
    error = detail if isinstance(detail, dict) else {}
    statuses = (event.get('api_error_status'), event.get('statusCode'), event.get('status'), error.get('status'))
    status = next((n for n in statuses if type(n) is int and n in CLOUDCLI_ERROR_STATUS), None)
    detail = error.get('message', detail)
    provider_status = anthropic_error(event, detail)
    if provider_status in CLOUDCLI_ERROR_STATUS:
        status = provider_status
    if status is None:
        match = re.match(r'^API Error:\s*([45]\d\d)\b', detail) if isinstance(detail, str) else None
        if match and int(match[1]) in CLOUDCLI_ERROR_STATUS:
            status = int(match[1])
        elif isinstance(detail, str) and detail.startswith('Not logged in') and '/login' in detail:
            status = 401
        else:
            status = 502
    return CloudCliError(f'CloudCLI agent reported an error (HTTP {status})' if status != 502
                         else 'CloudCLI agent did not report a successful result', status)


class CloudCliProgress:
    """Translate CloudCLI notices into visible text without creating client tool calls."""

    def __init__(self, stream):
        self.stream = stream
        self.pending = ''
        self.sent = 0
        self.notices = 0
        self.tools = {}
        self.thinking = False

    def notice(self, title, output=None):
        if self.notices >= 48:
            return
        self.notices += 1
        body = '\n\n> **SafeCode** · '+title
        if isinstance(output, str) and output.strip():
            preview = ''.join(c if c.isprintable() or c == '\n' else ' ' for c in output[:600])
            # Indented quoted code disables Markdown links and HTML in shell output.
            body += '\n>\n'+'\n'.join('>     '+line for line in preview.split('\n')[:10])
            if len(output) > 600 or preview.count('\n') >= 10:
                body += '\n>     …'
        self.stream.text(body+'\n\n')

    def feed(self, event):
        kind = event.get('kind')
        if kind == 'stream_delta':
            delta = event.get('content')
            if not isinstance(delta, str) or not delta:
                return
            self.pending += delta
            # Some providers surface an error as text. Leave its raw detail in CloudCLI.
            prefixes = ('API Error:', 'Not logged in')
            if not self.sent and any(p.startswith(self.pending) or self.pending.startswith(p) for p in prefixes):
                return
            self.stream.text(self.pending[self.sent:])
            self.sent = len(self.pending)
        elif kind == 'text':
            value = event['content']
            if self.pending.startswith(value):
                self.stream.text(value[self.sent:])
                self.pending = self.pending[len(value):]
                self.sent = max(0, self.sent-len(value))
            else:
                self.stream.text(value[self.sent:] if value.startswith(self.pending) else value)
                self.pending = ''
                self.sent = 0
            self.thinking = False
        elif kind == 'thinking' and not self.thinking:
            self.notice('Claude 正在思考')
            self.thinking = True
        elif kind == 'tool_use':
            tool, identifier = event.get('toolName'), event.get('toolId')
            if (isinstance(tool, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tool)
                and isinstance(identifier, str) and identifier not in self.tools and self.notices < 48):
                self.tools[identifier] = tool
                self.notice(f'正在运行 `{tool}`')
                self.thinking = False
        elif kind == 'tool_result':
            identifier = event.get('toolId')
            tool = self.tools.pop(identifier, None) if isinstance(identifier, str) else None
            if tool:
                status = '失败' if event.get('isError') is True else '已返回'
                output = event.get('content') if tool == 'Bash' else None
                self.notice(f'`{tool}` {status}', output)
        elif kind == 'task_status':
            change = event.get('event')
            phase = None
            if change == 'started':
                phase = '后台任务已启动'
            elif change == 'progress':
                phase = '后台任务进行中'
            elif change in ('notification', 'updated'):
                status = event.get('status')
                if status == 'completed':
                    phase = '后台任务已完成'
                elif status == 'failed':
                    phase = '后台任务失败'
                elif status == 'stopped':
                    phase = '后台任务已停止'
            if phase and (phase != '后台任务进行中' or self.notices < 12):
                self.notice(phase)


def cloudcli_events(response, stream=None):
    """Consume CloudCLI's one-line data events and require successful termination."""
    lines = total = 0
    text = []
    usage = dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0)
    session = None
    complete = False
    progress = CloudCliProgress(stream) if stream is not None else None
    deadline = time.monotonic() + 570
    while time.monotonic() < deadline:
        line = response.readline(MAX_REQUEST + 1)
        lines += 1
        total += len(line)
        diagnostic(streamLines=lines, receivedBytes=total)
        if not line:
            break
        if total > MAX_REQUEST or lines > 50000:
            raise CloudCliError('CloudCLI response exceeds the gateway limit')
        if not line.startswith(b'data:'):
            continue
        try:
            event = json.loads(line[5:])
        except (ValueError, UnicodeError):
            raise CloudCliError('CloudCLI returned an invalid event') from None
        if not isinstance(event, dict):
            raise CloudCliError('CloudCLI returned an invalid event')
        kind, event_type = event.get('kind'), event.get('type')
        if kind == 'error' or event_type == 'error' or event.get('is_error') is True:
            raise cloudcli_failure(event)
        if event_type == 'session-id':
            candidate = event.get('sessionId')
            if not isinstance(candidate, str) or not CLOUDCLI_SESSION_ID.fullmatch(candidate):
                raise CloudCliError('CloudCLI returned an invalid session identifier')
            if session is not None and session != candidate:
                raise CloudCliError('CloudCLI changed the run session identifier')
            if session is None and progress:
                progress.notice('已接收任务')
            session = candidate
        if kind == 'text' and event.get('role') == 'assistant' and not event.get('parentToolUseId'):
            content = event.get('content')
            if isinstance(content, str) and content.strip():
                if (event.get('isApiErrorMessage') or event.get('is_api_error_message')
                    or re.match(r'^API Error:\s*[45]\d\d\b', content)
                    or content.startswith('Not logged in') and '/login' in content):
                    raise cloudcli_failure(event)
                text.append({'type': 'text', 'text': content})
                if progress:
                    progress.feed(event)
        elif progress and kind != 'text' and not event.get('parentToolUseId'):
            progress.feed(event)
        if kind == 'status' and event.get('text') == 'token_budget':
            budget = event.get('tokenBudget')
            if isinstance(budget, dict):
                usage.update({dst: budget[src] for dst, src in CLOUDCLI_USAGE_FIELDS.items()
                              if type(budget.get(src)) is int and budget[src] >= 0})
        if kind == 'complete':
            if event.get('aborted') is True:
                raise CloudCliError('CloudCLI run was canceled', 409)
            if type(event.get('exitCode')) is not int or event['exitCode'] != 0 or event.get('success') is False:
                raise cloudcli_failure(event)
            complete = True
        if event_type == 'done':
            if session and complete and text:
                return session, stream.content if stream is not None else text, usage
            break
    raise CloudCliError('CloudCLI ended without a confirmed assistant reply')


class CloudCliBackend:
    project_lock = threading.Lock()

    def __init__(self, directory, config, device=None):
        self.config = config
        self.device = device
        self.parallel = False
        self.active_conversations = set()
        self.active_lock = threading.Lock()
        self.path = directory/'cloudcli-history.sqlite3'
        identity = {**config, 'apiKey': hashlib.sha256(config['apiKey'].encode()).hexdigest()}
        if config['hostControl'] is not None:
            # Workspace and shell settings do not change the identity of a saved conversation.
            identity['hostControl'] = {k: v for k, v in config['hostControl'].items()
                                       if k not in ('projectWorkspaces', 'allowSshShell')}
        self.identity = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (scope TEXT NOT NULL, history TEXT NOT NULL, '
                       'session TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (scope, history))')
            db.execute('CREATE TABLE IF NOT EXISTS session_projects (session TEXT PRIMARY KEY NOT NULL, '
                       'project TEXT NOT NULL, workspace TEXT, updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests (identity TEXT PRIMARY KEY, scope TEXT NOT NULL, '
                       'reply TEXT, created REAL NOT NULL)')

    def catalog(self):
        # This catalog is local configuration, not an inference or account probe.
        data = []
        families = set()
        for model in self.config['models']:
            family = model.split('-')[1]
            recognized = family in ('opus', 'sonnet', 'haiku', 'fable', 'mythos')
            default = recognized and family not in families
            families.add(family)
            data.append({'id': model, 'type': 'model', 'display_name': model,
                         'anthropic_family_tier': family if recognized else None,
                         'is_family_default': default, 'supported_effort_levels': [], 'supports_1m': False})
        return {'data': data, 'has_more': False, 'first_id': data[0]['id'], 'last_id': data[-1]['id']}

    def model(self, requested):
        if requested in self.config['models']:
            return requested
        if requested in ('opus', 'sonnet', 'haiku', 'fable'):
            for model in self.config['models']:
                if model.startswith('claude-'+requested+'-'):
                    return model
        raise CloudCliError('The requested model is not configured for the CloudCLI gateway', 404)

    def default_project(self):
        host = self.config['hostControl']
        workspace = host.get('projectWorkspaces', [host['workspace']])[0] if host else None
        return self.config['projectPath'], workspace

    def allowed_workspace(self, cwd):
        if self.device:
            path_type = PureWindowsPath if self.device['platform'] == 'windows' else PurePosixPath
            return str(cwd) if isinstance(cwd, path_type) else None
        host = self.config['hostControl']
        if host is None or not isinstance(cwd, PurePosixPath):
            return None
        roots = host.get('projectWorkspaces', [host['workspace']])
        return str(cwd) if any(cwd.is_relative_to(PurePosixPath(root)) for root in roots) else None

    def desktop_directory(self, data, scope):
        cwd = desktop_working_directory(data)
        if self.device:
            return cwd if self.allowed_workspace(cwd) else None
        host = self.config['hostControl']
        if not scope or not host or 'projectWorkspaces' not in host or self.allowed_workspace(cwd):
            return cwd
        # Desktop's bundled CLI can report a VM-side cwd. Resolve the corresponding
        # local session through the existing, pinned SafeCode control tunnel.
        try:
            result = subprocess.run(
                ['/usr/bin/python3', '-I', '-B', CLOUDCLI_MCP_SCRIPT, '--client-id', host['clientId'],
                 '--resolve-desktop-session', scope], capture_output=True, check=True, timeout=9)
            folder = json.loads(result.stdout).get('workspace')
            if valid_mac_workspace(folder):
                return PurePosixPath(folder)
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
        return cwd

    def project_for_directory(self, cwd):
        project, workspace = self.default_project()
        host = self.config['hostControl']
        adaptive = host is not None and 'projectWorkspaces' in host
        if not self.device and ((cwd is None and not adaptive) or (workspace is not None and cwd == PurePosixPath(workspace))):
            return project, workspace

        device = self.device['id'] if self.device else (host['clientId'] if host else str(self.path.parent))
        folder = cwd.as_posix() if cwd is not None else None
        if cwd is not None and cwd.drive:
            folder = folder.casefold()
        identity = hashlib.sha256(json.dumps([device, folder]).encode()).hexdigest()
        # CloudCLI uses the basename as the initial display name. Keep the
        # identity in a separate component so equally named folders stay distinct.
        label = cwd.name.encode()[:160].decode('utf-8', errors='ignore').strip() if cwd is not None else 'Desktop Chat'
        if not label or label in ('.', '..'):
            label = 'Desktop project'
        if self.device:
            label += ' · '+self.device['alias']
        root = Path.home()/CLOUDCLI_CONFIG.parent/'cloudcli-projects'
        bucket = root/identity
        try:
            for directory in (root.parent, root, bucket):
                self.private_project_directory(directory)
            # Save the name chosen on first use, including on case-insensitive clients.
            with self.project_lock:
                projects = list(bucket.iterdir())
                if len(projects) > 1:
                    raise ValueError()
                path = projects[0] if projects else bucket/label
                self.private_project_directory(path)
        except (OSError, ValueError):
            raise CloudCliError('SafeCode could not resolve the CloudCLI project directory', 409) from None
        return str(path), self.allowed_workspace(cwd)

    @staticmethod
    def private_project_directory(path):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError()

    @staticmethod
    def project_settings(path, value, *, remove=False):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if remove:
                return
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                staged = Path(stream.name)
                try:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.link(staged, path)
                finally:
                    staged.unlink(missing_ok=True)
            return
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or json.loads(stream.read(16384)) != value):
                raise ValueError()
        if remove:
            path.unlink()

    def prepare_project(self, project, workspace):
        if project == self.config['projectPath']:
            return
        path = Path(project)
        root = Path.home()/CLOUDCLI_CONFIG.parent/'cloudcli-projects'
        try:
            if path.parent.parent != root:
                raise ValueError()
            for directory in (root.parent, root, path.parent, path):
                self.private_project_directory(directory)
            if workspace is not None and not self.device:
                entry = self.host_tool_entry(workspace)
                entry['args'].append('--managed-project')
                with self.project_lock:
                    self.project_settings(path/'.mcp.json', {'mcpServers': {'safecode_host': entry}},
                                          remove=self.config['hostControl'].get('allowSshShell', False))
        except (OSError, ValueError):
            raise CloudCliError('SafeCode could not prepare the CloudCLI project directory', 409) from None

    def host_tool_entry(self, workspace):
        return {'type': 'stdio', 'command': '/usr/bin/python3',
                'args': ['-I', '-B', CLOUDCLI_MCP_SCRIPT, '--client-id', self.config['hostControl']['clientId'],
                         '--workspace', workspace]}

    def host_instruction(self, project, workspace):
        if self.device:
            system = 'Windows' if self.device['platform'] == 'windows' else 'macOS'
            directory = f'Working directory: {workspace}. ' if workspace else ''
            return (f"Target computer: {self.device['hostname']} ({system}). {directory}"
                    f"Connect using `{self.device['command']}` and execute commands on the target device.\n\nTask: ")
        host = self.config['hostControl']
        if host is None:
            return ''
        if workspace is None:
            return ('The selected desktop folder is outside the configured SafeCode file workspace. '
                    'The VPS filesystem is a different machine.\n\nTask: ')
        home = Path.home()
        client = host['clientId']
        allow_ssh_shell = host.get('allowSshShell', False)
        ssh_config = home/'.ssh/safe-code-host'/client/'config'
        tool_instruction = 'Use the SafeCode host tools for files on the selected computer. '
        try:
            if not ssh_config.is_file() or ssh_config.is_symlink() or not Path(CLOUDCLI_MCP_SCRIPT).is_file():
                raise ValueError()
            fd = os.open(ssh_config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError()
                if stream.readline(128) != '# Safe Code host bridge '+client+'\n':
                    raise ValueError()
                if allow_ssh_shell:
                    alias = re.fullmatch(r'Host ([a-z][a-z0-9_-]{0,31})\n', stream.readline(128))
                    if not alias or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise ValueError()
                    command = shlex.join(['ssh', '-F', str(ssh_config), alias[1]])
                    tool_instruction = (f'To perform the task on the target computer, connect with `{command}`. '
                                        'Execute the commands over SSH in the stated working directory on the Mac. ')
            if project == self.config['projectPath']:
                with (home/'.claude.json').open('r') as stream:
                    config = json.load(stream)
                tools = config.get('claudeProjects', {}).get(project, {}).get('mcpServers', {})
                expected = None if allow_ssh_shell else self.host_tool_entry(workspace)
                if expected is not None and 'projectWorkspaces' in host:
                    expected['args'].append('--managed-project')
                if tools.get('safecode_host') != expected:
                    raise ValueError()
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            raise CloudCliError('SafeCode local host control is unavailable for the CloudCLI project', 409) from None
        target = host['deviceName'] or 'the selected computer'
        return (f'Target computer: {target}. Working directory: {workspace}. '
                f'{tool_instruction}The VPS filesystem is a different machine.\n\nTask: ')

    def call(self, payload, stream=None):
        try:
            started = time.monotonic()
            with closing(HTTPConnection('127.0.0.1', self.config['port'], timeout=90)) as conn:
                diagnostic(stage='cloudcli_connect')
                conn.request('POST', '/api/agent', json.dumps(payload, ensure_ascii=False).encode(),
                             {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                              'x-api-key': self.config['apiKey']})
                with conn.getresponse() as response:
                    diagnostic(stage='cloudcli_response', cloudcliStatus=response.status,
                               cloudcliHeadersMs=min(900000, round((time.monotonic()-started)*1000)))
                    if response.status != 200:
                        status = response.status if response.status in CLOUDCLI_ERROR_STATUS else 502
                        if status in (401, 403):
                            raise CloudCliError('CloudCLI rejected the configured gateway credential', 403)
                        raise CloudCliError(f'CloudCLI Agent API returned HTTP {response.status}', status)
                    if response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'text/event-stream':
                        raise CloudCliError('CloudCLI Agent API returned an unexpected response')
                    return cloudcli_events(response, stream)
        except ConnectionAbortedError:
            raise
        except TimeoutError:
            raise CloudCliError('CloudCLI did not finish before the gateway timed out', 504) from None
        except (OSError, HTTPException):
            raise CloudCliError('CloudCLI connection ended before a result was confirmed') from None

    @contextmanager
    def conversation_slot(self, conversation):
        with self.active_lock:
            if conversation in self.active_conversations:
                raise CloudCliError('Another turn is already running in this CloudCLI conversation', 409)
            self.active_conversations.add(conversation)
        try:
            yield
        finally:
            with self.active_lock:
                self.active_conversations.remove(conversation)

    @contextmanager
    def configuration_lock(self):
        lockfile = Path.home()/CLOUDCLI_CONFIG.parent/'cloudcli-gateway.lock'
        lockfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'rb') as serial:
            lock_info = os.fstat(serial.fileno())
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid()
                or lock_info.st_mode & 0o077):
                raise CloudCliError('CloudCLI task lock is not private to the work account', 503)
            try:
                mode = fcntl.LOCK_SH if self.parallel else fcntl.LOCK_EX
                fcntl.flock(serial, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CloudCliError('CloudCLI is busy; wait for the current operation to finish', 409) from None
            yield

    def run(self, manager, data, context_1m, session_header, stream=None):
        if not isinstance(data, dict) or not isinstance(data.get('model'), str):
            raise CloudCliError('A configured CloudCLI model is required', 400)
        model = self.model(data['model'])
        if context_1m:
            raise CloudCliError('Explicit extended context is unavailable through CloudCLI', 400)
        original = data.get('messages')
        if not isinstance(original, list) or any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant')
                                                  for m in original):
            raise CloudCliError('Invalid CloudCLI messages', 400)
        messages = [m for m in original if m['role'] != 'system']
        if not messages or messages[-1]['role'] != 'user':
            raise CloudCliError('CloudCLI messages must end with a user task', 400)
        latest = blocks(messages[-1].get('content'))
        if any(not isinstance(b, dict) or b.get('type') != 'text' or not isinstance(b.get('text'), str) for b in latest):
            raise CloudCliError('CloudCLI only accepts user text; Desktop tool results and attachments are unsupported', 400)
        task = '\n'.join(re.sub(r'<system-reminder>.*?</system-reminder>', '', b['text'], flags=re.S)
                         for b in latest).strip()
        if not task:
            raise CloudCliError('CloudCLI requires text for the user task', 400)
        scope = conversation_scope(data, session_header)
        probe = (not scope and len(messages) == 1 and not data.get('tools') and not data.get('system')
                 and task in ('Hi', 'Hello') and data.get('max_tokens') == 1)
        desktop_title_task = (task.startswith('You are coming up with a succinct title and git branch name for a coding session ')
                              and 'Here is the session description:\n<description>' in task and '</description>' in task)
        if desktop_title_task or desktop_suggestion(messages):
            raise CloudCliError('Desktop background text generation is unavailable through CloudCLI', 400)
        effort = data.get('output_config', {})
        effort = effort.get('effort') if isinstance(effort, dict) else None
        if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
            raise CloudCliError('Unsupported Claude effort', 400)
        payload = {'provider': 'claude', 'message': task, 'model': model, 'stream': True}
        diagnostic(stage='session_route', modelFamily=model.split('-')[1])
        if effort is not None:
            payload['effort'] = effort
        # Scope is only used as a local lookup key. Never use a Desktop ID as a
        # CloudCLI session ID. Per-key gateway directories isolate these tables.
        namespace = self.identity+':'+scope
        before = history_signature(messages[:-1], 'cloudcli')
        now = time.time()
        if scope or len(messages)>1:
            identity = hashlib.sha256(json.dumps([namespace, before, task], ensure_ascii=False).encode()).hexdigest()
        elif probe:
            identity = hashlib.sha256(json.dumps([namespace, 'probe', model, int(now//300)]).encode()).hexdigest()
        else:
            identity = uuid.uuid4().hex
        conversation = namespace if scope else before if len(messages) > 1 else identity
        with self.conversation_slot(conversation), self.configuration_lock():
            with closing(sqlite3.connect(self.path)) as db, db:
                with db:
                    db.execute('DELETE FROM requests WHERE created<?', (now-30*86400,))
                    db.execute('DELETE FROM sessions WHERE created<?', (now-30*86400,))
                    db.execute('DELETE FROM session_projects WHERE updated<?', (now-30*86400,))
                prior = db.execute('SELECT reply FROM requests WHERE identity=?', (identity,)).fetchone()
                if prior is not None:
                    if prior[0] is None:
                        raise CloudCliError('This CloudCLI request was previously dispatched without a confirmed reply', 409)
                    return json.loads(prior[0])
                if len(messages) > 1:
                    saved = db.execute('SELECT sessions.session, project, workspace FROM sessions '
                                       'LEFT JOIN session_projects USING (session) WHERE scope=? AND history=?',
                                       (namespace, before)).fetchone()
                    if saved is None:
                        raise CloudCliError('No matching CloudCLI session; start a new Desktop conversation', 409)
                    payload['sessionId'], project, workspace = saved
                    default, default_workspace = self.default_project()
                    host = self.config['hostControl']
                    if self.device:
                        if not project:
                            raise CloudCliError('The paired session lacks a device project', 409)
                    elif not host or 'projectWorkspaces' not in host:
                        if project is None:
                            project, workspace = default, default_workspace
                    elif project is None or project == default:
                        cwd = self.desktop_directory(data, scope) if workspace != default_workspace else desktop_working_directory(data)
                        if cwd != PurePosixPath(default_workspace) and not (cwd is None and workspace == default_workspace):
                            raise CloudCliError('The original CloudCLI session uses another file workspace; start a new Desktop conversation', 409)
                        project, workspace = self.default_project()
                    else:
                        workspace = self.allowed_workspace(PurePosixPath(workspace)) if workspace else None
                        cwd = None if workspace else self.desktop_directory(data, scope)
                        if workspace is None and self.allowed_workspace(cwd):
                            routed_project, routed_workspace = self.project_for_directory(cwd)
                            if routed_project == project:
                                workspace = routed_workspace
                elif probe:
                    project, workspace = self.project_for_directory(None) if self.device else self.default_project()
                else:
                    project, workspace = self.project_for_directory(self.desktop_directory(data, scope))
                payload['projectPath'] = project
                if self.device or not probe:
                    if not probe:
                        payload['message'] = self.host_instruction(project, workspace)+task
                    self.prepare_project(project, workspace)
                # Commit before POST. A lost connection or process exit cannot
                # cause an automatic second dispatch of this scoped task.
                db.execute('INSERT INTO requests VALUES (?, ?, NULL, ?)', (identity, namespace, now))
            with manager.lock:
                manager.started += 1
            session, content, usage = self.call(payload, stream)
            if 'sessionId' in payload and session != payload['sessionId']:
                raise CloudCliError('CloudCLI unexpectedly changed the resumed session')
            result = {'id': stream.message_id if stream is not None else 'msg_'+uuid.uuid4().hex,
                      'type': 'message', 'role': 'assistant',
                      'model': data['model'], 'content': content, 'stop_reason': 'end_turn',
                      'stop_sequence': None, 'usage': usage}
            confirmed = history_signature(messages+[{'role': 'assistant', 'content': content}], 'cloudcli')
            with closing(sqlite3.connect(self.path)) as db, db:
                if not probe:
                    if len(messages) > 1:
                        db.execute('DELETE FROM sessions WHERE scope=? AND history=?', (namespace, before))
                    db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)', (namespace, confirmed, session, now))
                    if project != self.config['projectPath'] or (workspace is not None and 'projectWorkspaces' in self.config['hostControl']):
                        db.execute('INSERT OR REPLACE INTO session_projects VALUES (?, ?, ?, ?)',
                                   (session, project, workspace, now))
                    if scope:
                        db.execute('UPDATE requests SET reply=NULL WHERE scope=? AND identity<>?',
                                   (namespace, identity))
                db.execute('UPDATE requests SET reply=? WHERE identity=?',
                           (json.dumps(result, ensure_ascii=False), identity))
            manager.count_usage({'id': result['id'], 'model': model, 'usage': usage}, result['id'])
            return result


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
                            native_error=line.get('error')
                            for block in blocks(line.get('message', {}).get('content', [])):
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    anthropic_error(line, block.get('text'))
                            continue
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
                            else:
                                status = anthropic_error(line)
                                raise GatewayError('VPS Claude Code could not complete the model request', status or 502)
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
        settings = config['cloudcli'] if 'cloudcli' in config else load_cloudcli_config()
        if settings is not None:
            settings = cloudcli_config(settings)
        self.cloudcli = CloudCliBackend(self.directory, settings, config.get('pairedDevice')) if settings is not None else None
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

    def catalog(self):
        if self.cloudcli is not None:
            return self.cloudcli.catalog()
        with self.models_lock:
            if self.models is None:
                self.models = native_model_catalog()
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

    def count_input_tokens(self, data, headers=None):
        raise GatewayError('Token counting is unavailable on this CLI bridge', 501)

    def run(self, data, context_1m=False, session_header=None, stream=None):
        if self.cloudcli is not None:
            return self.cloudcli.run(self, data, context_1m, session_header, stream)
        requested_model = data.get('model') if isinstance(data, dict) else None
        data=validate_request(data)
        diagnostic(stage='cli_turn')
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


class CloudCliMessagesStream:
    def __init__(self, handler, model):
        self.handler = handler
        self.model = model
        self.message_id = 'msg_'+uuid.uuid4().hex
        self.started = False
        self.parts = []

    @property
    def content(self):
        return [{'type': 'text', 'text': ''.join(self.parts)}]

    def text(self, part):
        if not part:
            return
        try:
            if not self.started:
                self.started = True
                self.handler.start_sse()
                self.handler.send_sse('message_start', {'type': 'message_start', 'message': {
                    'id': self.message_id, 'type': 'message', 'role': 'assistant', 'model': self.model,
                    'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0),
                }})
                self.handler.send_sse('content_block_start', {
                    'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''},
                })
            self.handler.send_sse('content_block_delta', {
                'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': part},
            })
            self.parts.append(part)
        except OSError:
            raise ConnectionAbortedError('Desktop disconnected during a CloudCLI stream') from None

    def finish(self, response):
        self.handler.send_sse('content_block_stop', {'type': 'content_block_stop', 'index': 0})
        for event, payload in sse_tail(response):
            self.handler.send_sse(event, payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):pass

    @property
    def manager(self):
        return self.server.manager

    def authorized(self):
        token = self.headers.get('Authorization', '')
        token = token[7:] if token.startswith('Bearer ') else self.headers.get('x-api-key', '')
        return hmac.compare_digest(token.encode(), self.manager.config['token'].encode())

    def diagnostic_result(self, code, error_type):
        trace = REQUEST_DIAGNOSTIC.get()
        if trace is None:
            return None
        elapsed = min(900000, round((time.monotonic() - self.diagnostic_started) * 1000))
        return {**trace, 'gatewayStatus': code,
                'errorType': error_type if error_type in REJECTION_TYPES else 'api_error',
                'gatewayMs': elapsed}

    @contextmanager
    def request_diagnostic(self, backend, **state):
        requested = self.headers.get('X-SafeCode-Diagnostics') == '1'
        self.diagnostic_started = time.monotonic()
        initial = dict(id=str(uuid.uuid4()), protocol=1, backend=backend, stage='gateway_auth', **state) if requested else None
        context = REQUEST_DIAGNOSTIC.set(initial)
        try:
            yield
        finally:
            REQUEST_DIAGNOSTIC.reset(context)

    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if code >= 400 and (report := self.diagnostic_result(code, data.get('error', {}).get('type', 'api_error'))):
            self.send_header(DIAGNOSTIC_HEADER, json.dumps(report, separators=(',', ':')))
        self.end_headers()
        self.wfile.write(body)

    def start_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

    def send_sse(self, event, payload):
        self.wfile.write(('event: '+event+'\ndata: '+json.dumps(payload, ensure_ascii=False)+'\n\n').encode())
        self.wfile.flush()

    def message_error(self, stream, code, error_type, message):
        payload = {'type': 'error', 'error': {'type': error_type, 'message': message}}
        try:
            if stream is not None and stream.started:
                report = self.diagnostic_result(code, error_type)
                if report is not None:
                    payload['safeCodeDiagnostic'] = report
                self.send_sse('error', payload)
            else:
                self.reply(code, payload)
        except OSError:
            pass

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
        backend = 'cloudcli' if self.manager.cloudcli is not None else 'claude_code'
        with self.request_diagnostic(backend):
            self.handle_messages()

    def handle_messages(self):
        self.close_connection = True
        if not self.authorized():return self.reply(401, {'error': {'type': 'authentication_error', 'message': 'Local bridge token required'}})
        if self.path == '/shutdown':
            self.reply(200, {'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        path = self.path.partition('?')[0]
        if path not in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            return self.reply(501, {'error': {'type': 'api_error', 'message': 'This endpoint is not supported by the VPS CLI bridge'}})
        stream = None
        try:
            diagnostic(stage='request_decode')
            if self.headers.get('Transfer-Encoding'):raise GatewayError('Content-Length required', 400)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:raise GatewayError('Request too large or empty', 413)
            self.connection.settimeout(30)
            diagnostic(requestBytes=length)
            data = json.loads(self.rfile.read(length))
            diagnostic(stage='gateway_validation')
            if isinstance(data, dict):
                for field, name in (('messages', 'messageCount'), ('tools', 'toolCount')):
                    if isinstance(data.get(field), list):
                        diagnostic(**{name: min(1000000, len(data[field]))})
            if path == COUNT_TOKENS_PATH:
                headers = {name: self.headers[name] for name in ('anthropic-version', 'anthropic-beta')
                           if name in self.headers}
                return self.reply(200, self.execute_message(data, count_headers=headers))
            if isinstance(data, dict) and data.get('stream') and self.manager.cloudcli is not None:
                stream = CloudCliMessagesStream(self, data.get('model'))
            response = self.execute_message(data, stream=stream, session_header=self.headers.get('x-claude-code-session-id'),
                context_1m='context-1m-2025-08-07' in {v.strip() for v in self.headers.get('anthropic-beta', '').split(',')})
            if stream is not None and stream.started:
                stream.finish(response)
            elif data.get('stream'):
                self.start_sse()
                for event, payload in sse_response(response):
                    self.send_sse(event, payload)
            else:self.reply(200, response)
        except GatewayError as error:
            self.message_error(stream, error.status, getattr(error, 'error_type', 'api_error'), str(error))
        except (ValueError, TypeError, KeyError, AttributeError):
            self.message_error(stream, 400, 'invalid_request_error', 'Invalid Messages request')
        except sqlite3.Error:
            self.message_error(stream, 503, 'api_error', 'Desktop conversation history unavailable')
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
    yield from sse_tail(response)


def sse_tail(response):
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
