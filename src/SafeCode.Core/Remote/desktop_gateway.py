#!/usr/bin/env python3
"""Claude Desktop Messages API → 现有 VPS 桥接守护进程 → 原版 Claude Code。

模型请求由远端 claude 进程发出。桥接服务不读取上游模型密钥。
状态、会话及正文只在工作用户范围内；HTTP 仅回环监听，需独立随机 token。
"""
import argparse
import base64
import binascii
import copy
from contextlib import contextmanager, closing, nullcontext, suppress
from itertools import groupby
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
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
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlsplit

MAX_REQUEST = 8 * 1024 * 1024
MAX_MESSAGES_REQUEST = 64 * 1024 * 1024
MAX_CLOUDCLI_REPLY = 32 * 1024 * 1024
MAX_CLOUDCLI_ATTACHMENT = 10 * 1024 * 1024  # CloudCLI /api/assets/files per-file limit
CLOUDCLI_SSE_PING_INTERVAL = 15
API_ONLY_TURN_RUNNING = '上一轮仍在 CloudCLI 中执行。请等待完成后在本对话重试；如需立即停止，请在 CloudCLI 中停止上一轮。'
CLOUDCLI_START_TIMEOUT = 120
CLOUDCLI_HISTORY_LIMIT = 4 * 1024 * 1024
MESSAGES_PATH = '/v1/messages'
COUNT_TOKENS_PATH = MESSAGES_PATH + '/count_tokens'
MODEL = re.compile(r'^(?:claude-[a-z0-9]+(?:-[a-z0-9]+)*|fable|opus|sonnet|haiku)(?:\[1m\])?$')
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max')
CLOUDCLI_EFFORTS = EFFORTS | {'ultracode'}
CLOUDCLI_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}(?:\[1m\])?')
CLAUDE_MODEL_FAMILIES = ('opus', 'sonnet', 'haiku', 'fable', 'mythos')
CLOUDCLI_MODEL_SELECTORS = ('default', 'best', 'opusplan')
DESKTOP_MODEL_ALIAS_PREFIX = 'safecode-cloudcli-'
DESKTOP_ULTRACODE_PREFIX = 'safecode-ultracode/'


def cloudcli_model_family(model):
    base = model.removesuffix('[1m]')
    return next((family for family in CLAUDE_MODEL_FAMILIES
                 if base == family or base.startswith('claude-'+family+'-')), None)


def native_model_catalog(home=None, timeout=25, *, include_options=False):
    """Read the installed CLI's initialize response, without sending a model prompt.

    Do not export account/identity data from that response. Versions and effort
    capabilities come from the CLI rather than a fixed list in the desktop app.
    """
    # TODO: Remove this direct CLI probe once the gateway reads the catalog from CloudCLI.
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
                    return model_catalog(rows, home, include_options=include_options)
        raise GatewayError('Native Claude Code model discovery did not complete')
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL);child.wait()
        child.stdin.close();child.stdout.close()


def model_catalog(rows, home, *, include_options=False):
    models = {}
    aliases = {}
    for row in rows:
        if not isinstance(row, dict):continue
        identity = row.get('resolvedModel', '')
        if not isinstance(identity, str) or not identity.startswith('claude-') or not MODEL.fullmatch(identity):continue
        alias = row.get('value', '')
        if not isinstance(alias, str):continue
        if alias == 'default' and any(isinstance(r, dict) and r.get('resolvedModel') == identity
                                      and r.get('value') != 'default' for r in rows):continue
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
        if not isinstance(row, dict):continue
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
    if not models and not include_options:raise GatewayError('Native Claude Code returned no model choices')
    result = {'data': list(models.values()), 'has_more': False,
              'first_id': next(iter(models), None), 'last_id': next(reversed(models), None)}
    if include_options:
        options = {}
        for row in (*extras, *rows):
            if not isinstance(row, dict):continue
            model = row.get('value')
            if not isinstance(model, str) or not CLOUDCLI_MODEL_ID.fullmatch(model):continue
            option = {'label': row.get('displayName', row.get('label')), 'description': row.get('description')}
            levels = row.get('supportedEffortLevels')
            if isinstance(levels, list):
                option['effort'] = {'values': [{'value': level} for level in levels if isinstance(level, str)]}
            resolved = row.get('resolvedModel')
            if isinstance(resolved, str) and resolved.startswith('claude-') and MODEL.fullmatch(resolved):
                option['behaves_as'] = resolved.removesuffix('[1m]')
            options[model] = option
        result['options'] = options
    return result


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
    evidence = 'cli_event' if status is not None else None
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
    if status is not None or kind is not None or request is not None:
        fields = {'anthropicEvidence': evidence, 'anthropicStatus': status,
                  'anthropicError': kind, 'anthropicRequestId': request}
        diagnostic(stage='anthropic_response', **{key: value for key, value in fields.items() if value is not None})
    return status


class GatewayError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


CLOUDCLI_CONFIG = Path('.config/safe-code/cloudcli-gateway.json')
DEFAULT_CLOUDCLI_PROJECT = Path('.local/share/safe-code/cloudcli-workspace')
DEFAULT_CLOUDCLI_MODELS = ('claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5')
CLOUDCLI_REQUIRED = '请先在当前 VPS 配置 CloudCLI 后端。Safe Code 的 Claude Desktop 接入需要 CloudCLI。'
CLOUDCLI_MCP_SCRIPT = '/opt/safe-code/desktop-api/host_control_mcp.py'
CLOUDCLI_SESSION_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}')
CLOUDCLI_ERROR_STATUS = {400, 401, 402, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504, 529}
CLOUDCLI_USAGE_FIELDS = {
    'input_tokens': 'inputTokens',
    'output_tokens': 'outputTokens',
    'cache_creation_input_tokens': 'cacheCreationTokens',
    'cache_read_input_tokens': 'cacheReadTokens',
}
CLOUDCLI_MEDIA = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
                  'application/pdf': '.pdf', 'text/plain': '.txt', 'text/markdown': '.md',
                  'text/csv': '.csv', 'text/tab-separated-values': '.tsv', 'application/json': '.json'}
SYSTEM_REMINDER = re.compile(r'<system-reminder>.*?</system-reminder>', re.S)
DESKTOP_LOCAL_COMMAND = re.compile(
    r'<(local-command-caveat|local-command-stdout|command-name|command-message|command-args)>.*?</\1>', re.S)
DESKTOP_MODEL_COMMAND = re.compile(
    r'<command-name>[ \t]*/model[ \t]*</command-name>\s*'
    r'<command-message>[ \t]*model[ \t]*</command-message>\s*'
    r'<command-args>[^<]{0,256}</command-args>')
DESKTOP_MODEL_NOTICE = re.compile(
    r"<local-command-caveat>The command below was run directly in Claude Code, not sent to you as a request, "
    r"and its output goes straight to the user\. It's recorded here as context for later messages\.</local-command-caveat>"
    r"|<local-command-stdout>Set model to [^<\r\n]{1,256}</local-command-stdout>")
DESKTOP_COMPACT_PREFIX = ('This session is being continued from a previous conversation that ran out of context. '
                          'The summary below covers the earlier portion of the conversation.')
PLAN_MODE_REMINDER = re.compile(r'<system-reminder>\s*Plan mode is active\. The user indicated that they do not want you to execute yet\b')


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


def require_cloudcli_config():
    try:
        config = load_cloudcli_config()
    except GatewayError:
        raise GatewayError('VPS 上的 CloudCLI 配置无效，请检查工作账户的网关配置。', 503) from None
    if config is None:
        raise GatewayError(CLOUDCLI_REQUIRED, 503)
    return config


def verify_cloudcli_agent(config):
    """The authenticated Agent route rejects an empty body before preparing a turn."""
    try:
        with closing(HTTPConnection('127.0.0.1', config['port'], timeout=5)) as conn:
            conn.request('POST', '/api/agent', b'{}', {'Content-Type': 'application/json', 'x-api-key': config['apiKey']})
            with conn.getresponse() as response:
                if response.status in (401, 403):
                    raise GatewayError('CloudCLI 拒绝了网关使用的 API Key，请检查 CloudCLI 的密钥设置。', 503)
                if response.status == 400 and json.loads(response.read(4096)).get('error') == 'Either githubUrl or projectPath is required':
                    return
    except (OSError, HTTPException, ValueError, AttributeError):
        pass
    raise GatewayError('无法验证 VPS 上的 CloudCLI Agent API，请检查 CloudCLI 是否已安装并运行。', 503)


def cloudcli_web_origin(origin):
    if origin is None:
        return None
    if not isinstance(origin, str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?', origin):
        raise ValueError('CloudCLI web address must be an HTTPS origin without a path or credentials.')
    try:
        if urlsplit(origin).port != 0:
            return origin.rstrip('/')
    except ValueError:
        pass
    raise ValueError('CloudCLI web address has an invalid port.')


def cloudcli_extended_context(value, models):
    if (not isinstance(value, list) or len(value) > len(models)
            or any(not isinstance(model, str) or model not in models for model in value)
            or len(set(value)) != len(value)):
        raise ValueError('Each 1M context model must be in the CloudCLI model allowlist.')
    return value


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
        not isinstance(model, str) or not CLOUDCLI_MODEL_ID.fullmatch(model) or model.endswith('[1m]') for model in models
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
    questions = value.get('desktopQuestions')
    if questions is not None:
        if not isinstance(questions, dict):
            raise ValueError()
        if 'tokenFile' in questions:
            if set(questions) != {'tokenFile'} or not isinstance(questions['tokenFile'], str):
                raise ValueError()
            token_file = PurePosixPath(questions['tokenFile'])
            if token_file == PurePosixPath('/run') or not token_file.is_relative_to('/run') or '..' in token_file.parts:
                raise ValueError()
            questions = {'tokenFile': str(token_file)}
        else:
            if not all(isinstance(questions.get(field), str) and 0 < len(questions[field]) <= 512
                       for field in ('username', 'password')):
                raise ValueError()
            questions = {field: questions[field] for field in ('username', 'password')}
    tool_approvals = value.get('desktopToolApprovals', False)
    if type(tool_approvals) is not bool or (tool_approvals and questions is None):
        raise ValueError('Desktop tool approvals require the authenticated CloudCLI connection.')
    extended_context = cloudcli_extended_context(value.get('extendedContextModels', []), models)
    return {'port': port, 'apiKey': api_key, 'projectPath': str(Path(project).resolve()), 'models': models,
            'hostControl': host, 'webOrigin': cloudcli_web_origin(value.get('webOrigin')), 'desktopQuestions': questions,
            'extendedContextModels': extended_context, 'desktopToolApprovals': tool_approvals}


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
    digest = hashlib.sha256()
    for piece in json.JSONEncoder(sort_keys=True, ensure_ascii=False).iterencode(normalized(messages)):
        digest.update(piece.encode())
    return digest.hexdigest()


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


def desktop_model_probe(data, messages, content):
    maximum = data.get('max_tokens')
    if (type(maximum) is not int or maximum != 1 or len(messages) != 1 or data.get('tools')
            or len(content) != 1 or content[0].get('text') not in ('.', 'Hi', 'Hello')):
        return False
    system = data.get('system')
    if not system:
        return True
    # Native model validation may include only the CLI identity and attribution.
    if not isinstance(system, list) or not 1 <= len(system) <= 2:
        return False
    if not all(isinstance(block, dict) and block.get('type') == 'text'
               and isinstance(block.get('text'), str) for block in system):
        return False
    identity = "You are Claude Code, Anthropic's official CLI for Claude."
    return (system[-1]['text'] == identity
            and (len(system) == 1 or system[0]['text'].startswith('x-anthropic-billing-header: ')))


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


DESKTOP_TITLE_PREFIXES = (
    'You are coming up with a succinct title for an agent chat session ',
    'You are coming up with a succinct title and git branch name for a coding session ',
)
DESKTOP_TITLE_DESCRIPTION = 'Here is the session description:\n<description>'


def cloudcli_title_response(task):
    """Answer Desktop's tagged metadata request without starting a CloudCLI chat."""
    if not task.startswith(DESKTOP_TITLE_PREFIXES) or DESKTOP_TITLE_DESCRIPTION not in task:
        return None
    description = task.split(DESKTOP_TITLE_DESCRIPTION, 1)[1]
    match = re.fullmatch(r'(.*)</description>\nPlease generate a title(?: and branch name)? for this session[.,].*',
                         description, flags=re.S)
    if match is None:
        raise CloudCliError('Unrecognized Desktop title request', 400)
    description = match[1].replace('<', '').replace('>', '')
    label = desktop_title([{'role': 'user', 'content': ' '.join(description.split()[:5])}])
    response = f'<title>{label}</title>'
    if task.startswith(DESKTOP_TITLE_PREFIXES[1]):
        words = re.findall(r'[a-z0-9]+', label.lower())[:3]
        suffix = hashlib.sha256(description.encode()).hexdigest()[:8]
        slug = '-'.join(words)[:40].rstrip('-') or 'session'
        response += f'\n<branch>claude/{slug}-{suffix}</branch>'
    return response


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


def without_desktop_model_commands(messages):
    """Remove the Desktop /model transcript records while preserving the task and tools."""
    clean = []
    changed = False
    for role, items in groupby(messages, key=lambda message: message['role']):
        group = list(items)
        if role != 'user':
            clean.extend(group)
            continue
        contents = [blocks(message.get('content', [])) for message in group]
        if not any(DESKTOP_MODEL_COMMAND.search(item.get('text', ''))
                   for content in contents for item in content if item.get('type') == 'text'):
            clean.extend(group)
            continue
        changed = True
        for message, content in zip(group, contents):
            filtered = []
            for item in content:
                if item.get('type') == 'text':
                    text = item.get('text', '')
                    value = DESKTOP_MODEL_NOTICE.sub('', DESKTOP_MODEL_COMMAND.sub('', text))
                    if value != text:
                        if not value.strip():
                            continue
                        item['text'] = value
                filtered.append(item)
            if filtered or not content:
                clean.append({**message, 'content': filtered} if content else message)
    return clean if changed else messages


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
                value = SYSTEM_REMINDER.sub('', item['text'])
                if not value.strip():
                    continue
                item['text'] = value
            visible.append(item)
        if visible:
            clean.append({'role':msg['role'], 'content':visible})
    return signature(clean)


DESKTOP_INTERRUPTIONS = frozenset(('[Request interrupted by user]', '[Request interrupted by user for tool use]'))


def desktop_assistant_text(message):
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return None
    visible = [item for item in blocks(message.get('content', []))
               if item.get('type') not in ('thinking', 'redacted_thinking')]
    if any(item.get('type') != 'text' or not isinstance(item.get('text'), str) for item in visible):
        return None
    return ''.join(item['text'] for item in visible)


def split_desktop_interruptions(messages):
    """Restore the turn boundaries lost when Desktop coalesces consecutive user messages."""
    result, stopped, interrupted_replies = [], set(), set()
    for message in messages:
        if message['role'] != 'user':
            result.append(message)
            continue
        content = blocks(message.get('content', []))
        if not any(item.get('type') == 'text' and item.get('text', '').strip() in DESKTOP_INTERRUPTIONS
                   for item in content):
            result.append(message)
            continue
        pending = []
        for item in content:
            if item.get('type') != 'text' or item.get('text','').strip() not in DESKTOP_INTERRUPTIONS:
                pending.append(item)
                continue
            if pending:
                if pending[-1].get('type') == 'text':
                    pending[-1]['text'] = pending[-1]['text'].removesuffix('\n')
                stopped.add(len(result))
                result.append({'role':'user','content':pending})
                pending = []
            elif result and result[-1]['role'] == 'user':
                stopped.add(len(result)-1)
            elif result and result[-1]['role'] == 'assistant':
                interrupted_replies.add(len(result)-1)
        if pending:
            result.append({'role':'user','content':pending})
    if len(result)-1 in stopped:
        raise CloudCliError('CloudCLI requires a new user task after stopping', 400)
    # A rejected attempt can still appear in the UI even though the gateway
    # never ran it. Try earlier checkpoints only through marked user turns.
    prefixes = []
    stopped_requests = set()
    index = len(result)-2
    while index in stopped:
        prefixes.append(history_signature(result[:index],'cloudcli'))
        stopped_requests.add(history_signature(result[:index+1], 'cloudcli'))
        index -= 1
    # Desktop may display its own placeholder after a canceled local attempt.
    # Ignore it here only next to an explicit stop while locating the partial reply.
    while index > 0:
        if index in stopped:
            stopped_requests.add(history_signature(result[:index+1], 'cloudcli'))
            index -= 1
        elif ((index-1 in stopped or index-1 in interrupted_replies)
              and desktop_assistant_text(result[index]) == 'No response requested.'):
            index -= 1
        else:
            break
    partial_history = result[:index+1] if index in interrupted_replies else None
    if partial_history:
        stopped_requests.add(history_signature(partial_history[:-1], 'cloudcli'))
    return result, prefixes, partial_history, stopped_requests


def desktop_compaction_request(system, task):
    instruction = 'You are a helpful AI assistant tasked with summarizing conversations.'
    if isinstance(system, str):
        system = [{'type': 'text', 'text': system}]
    if not isinstance(system, list):
        return False
    system_text = [part['text'] for part in system if isinstance(part, dict)
                   and part.get('type') == 'text' and isinstance(part.get('text'), str)]
    # The native client may reuse the main agent system prompt for compaction.
    # In that form require both the client header and its closing instructions.
    native_prompt = (any(text.startswith('x-anthropic-billing-header: cc_version=') for text in system_text)
                     and 'REMINDER: Do NOT call any tools. Respond with plain text only' in task
                     and 'Tool calls will be rejected and you will fail the task.' in task)
    return ((instruction in system_text or native_prompt)
            and task.startswith('CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.')
            and 'Your task is to create a detailed summary of the conversation so far' in task
            and '<summary>' in task)


def desktop_compact_context(messages):
    for message in messages:
        if message['role'] != 'user':
            return None
        content = blocks(message.get('content'))
        if not content or any(item.get('type') != 'text' for item in content):
            return None
        text = ''.join(item['text'] for item in content)
        text = text.lstrip()
        while (reminder := SYSTEM_REMINDER.match(text)):
            text = text[reminder.end():].lstrip()
        if text.startswith(DESKTOP_COMPACT_PREFIX+'\n'):
            _, marker, body = text.partition('\nSummary:\n')
            if marker:
                return body.lstrip()
        if SYSTEM_REMINDER.sub('', text).strip():
            return None
    return None


def split_compact_followup(messages):
    # The Messages API allows adjacent user turns to be combined into blocks.
    first = messages[0]
    content = first.get('content')
    if first['role'] == 'user' and isinstance(content, list) and len(content) > 1:
        for index, item in enumerate(content[:-1]):
            if item.get('type') != 'text':
                break
            text = item.get('text', '')
            if not SYSTEM_REMINDER.sub('', text).strip():
                continue
            wrapper = {**first, 'content': content[:index+1]}
            if desktop_compact_context([wrapper]):
                return [wrapper, {**first, 'content': content[index+1:]}, *messages[1:]]
            break
    return messages


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


class CloudCliStopped(ConnectionAbortedError):
    """CloudCLI confirmed that the execution owned by the Desktop stream stopped."""


class CloudCliNativeStop(CloudCliError):
    """The upstream response confirmed a stop initiated outside this gateway."""


class CloudCliRejected(CloudCliError):
    """CloudCLI explicitly refused the request before starting it."""


class CloudCliEditRefused(CloudCliRejected):
    """CloudCLI refused an edit before changing the transcript."""


class CloudCliWebResponseTooLarge(CloudCliError):
    """A native web response exceeded its read budget."""


def cloudcli_web_login(port, credentials):
    try:
        with closing(HTTPConnection('127.0.0.1', port, timeout=8)) as conn:
            conn.request('POST', '/api/auth/login', json.dumps(credentials).encode(),
                         {'Content-Type': 'application/json'})
            with conn.getresponse() as response:
                if response.status != 200:
                    raise CloudCliError('CloudCLI could not authenticate Desktop questions', 503)
                data = json.loads(response.read(16384))
        token = data.get('token')
        if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
            return token
    except (OSError, HTTPException, ValueError, AttributeError):
        pass
    raise CloudCliError('CloudCLI could not authenticate Desktop questions', 503)


def cloudcli_local_token(path):
    """Read a short-lived CloudCLI session issued by the local administrator."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError()
            raw = stream.read(4097)
        token = raw.decode('ascii').strip()
        if len(raw) <= 4096 and re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
            claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
            if isinstance(claims, dict) and type(claims.get('exp')) is int and 0 < claims['exp']-time.time() <= 300:
                return token
    except (OSError, ValueError, RecursionError):
        pass
    raise CloudCliError('CloudCLI desktop session token is unavailable or expired', 503)


def cloudcli_websocket():
    try:
        from websocket import create_connection, WebSocketException
    except ImportError:
        raise CloudCliError('Desktop questions require the websocket-client package on the gateway', 503) from None
    return create_connection, WebSocketException


def cloudcli_attachment(block, number):
    source = block.get('source')
    source = source if isinstance(source, dict) else {}
    media, encoding, value = source.get('media_type'), source.get('type'), source.get('data')
    if media is None and encoding == 'text':
        media = 'text/plain'
    if not isinstance(media, str) or media not in CLOUDCLI_MEDIA or not isinstance(value, str):
        raise CloudCliError('CloudCLI supports inline PNG, JPEG, GIF, WebP, PDF and text attachments', 400)
    image = block.get('type') == 'image'
    if image != media.startswith('image/') or (encoding != 'base64' and (encoding != 'text' or not media.startswith('text/'))):
        raise CloudCliError('CloudCLI attachments must contain inline image, PDF or text data', 400)
    try:
        data = base64.b64decode(value, validate=True) if encoding == 'base64' else value.encode()
    except (ValueError, binascii.Error, UnicodeError):
        raise CloudCliError('Desktop sent invalid attachment data', 400) from None
    if not data:
        raise CloudCliError('Desktop sent an empty attachment', 400)
    if len(data) > MAX_CLOUDCLI_ATTACHMENT:
        raise CloudCliError('Desktop attachment exceeds 10 MiB', 413)
    name = block.get('title')
    name = re.sub(r'[^\w.-]+', '_', name).strip('._')[:100] if isinstance(name, str) else ''
    if not name:
        name = f'attachment-{number}'
    extension = CLOUDCLI_MEDIA[media]
    if not name.lower().endswith(extension):
        name += extension
    return name, media, data


def desktop_read_attachments(messages, task, first_number):
    # Claude Desktop represents files opened in its composer as a native Read
    # preview. Only pair explicit references in this turn with their preview.
    candidates = []
    for message in reversed(messages):
        if message['role'] != 'user':
            break
        texts = [block.get('text') for block in blocks(message.get('content')) if block.get('type') == 'text']
        if candidates and (not texts or any(SYSTEM_REMINDER.sub('', text).strip()
                                            for text in texts if isinstance(text, str))):
            break
        candidates[:0] = [text for text in texts if isinstance(text, str)]
    files = []
    path = None
    for text in candidates:
        input_prefix = '<system-reminder>\nCalled the Read tool with the following input: '
        result_prefix = '<system-reminder>\nResult of calling the Read tool:\n'
        trailer = '\n</system-reminder>'
        if text.startswith(input_prefix) and text.endswith(trailer):
            path = None
            try:
                candidate = json.loads(text[len(input_prefix):-len(trailer)]).get('file_path')
            except (ValueError, AttributeError):
                continue
            if (valid_mac_workspace(candidate) and f'@"{candidate}"' in task
                    and PurePosixPath(candidate).name.lower().endswith(('.txt', '.md', '.csv', '.tsv', '.json'))):
                path = candidate
        elif path and text.startswith(result_prefix) and text.endswith(trailer):
            preview = text[len(result_prefix):-len(trailer)]
            lines = preview.split('\n')
            if lines and all(re.match(r'^\s*\d+\t', line) for line in lines):
                contents = '\n'.join(line.partition('\t')[2] for line in lines)
                if contents:
                    files.append(cloudcli_attachment({'type': 'document', 'title': PurePosixPath(path).name,
                        'source': {'type': 'text', 'data': contents}}, first_number+len(files)))
            path = None
    return files


def cloudcli_failure(event, progress=None):
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
            if progress is not None:
                return progress.incomplete('CloudCLI agent did not report a successful result')
            status = 502
    return CloudCliError(f'CloudCLI agent reported an error (HTTP {status})' if status != 502
                         else 'CloudCLI agent did not report a successful result', status)


@dataclass
class CloudCliApproval:
    session: str
    request_id: str
    original: dict
    block: dict
    plan: Optional[str] = None
    upstream_tool: Optional[str] = None

    @property
    def tool_name(self):
        return self.upstream_tool or self.block['name']

    @classmethod
    def read(cls, event, session, native_tools, plan_candidate, tool_approvals=False):
        request_id, original = event.get('requestId'), event.get('input')
        tool = event.get('toolName')
        if (not session or event.get('sessionId') != session or not isinstance(tool, str)
                or not isinstance(request_id, str) or not CLOUDCLI_SESSION_ID.fullmatch(request_id)
                or not isinstance(original, dict)):
            return None
        if tool not in ('AskUserQuestion', 'ExitPlanMode', 'McpElicitation'):
            if (not tool_approvals or 'AskUserQuestion' not in native_tools
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', tool)
                    or len(json.dumps(original, ensure_ascii=False).encode()) > 24000):
                return None
            question = {'question': '是否允许 CloudCLI 执行上面显示的这一次操作？',
                        'header': '远端授权', 'multiSelect': False, 'options': [
                            {'label': '允许一次', 'description': '由 CloudCLI 执行这一项操作'},
                            {'label': '拒绝', 'description': '拒绝这一项操作'},
                        ]}
            fields = {'type': 'tool_use', 'id': 'toolu_'+uuid.uuid4().hex, 'name': 'AskUserQuestion',
                      'input': {'questions': [question]}}
            return cls(session, request_id, original, fields, upstream_tool=tool)
        if tool not in native_tools:
            return None
        if tool == 'ExitPlanMode':
            plan = original.get('plan')
            if plan is None and plan_candidate is not None:
                summary = {key: value for key, value in plan_candidate.items() if key != 'plan'}
                if summary == original:
                    plan = plan_candidate['plan']
            if (not isinstance(plan, str) or not plan.strip() or len(plan.encode()) > 24000
                    or len(json.dumps(original, ensure_ascii=False).encode()) > 28000):
                return None
            fields = {'type': 'tool_use', 'id': 'toolu_'+uuid.uuid4().hex, 'name': tool,
                      'input': {'allowedPrompts': []}}
            return cls(session, request_id, original, fields, plan)
        questions = original.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
            return None
        clean = []
        for item in questions:
            if (not isinstance(item, dict) or not isinstance(item.get('question'), str) or not item['question'].strip()
                    or not isinstance(item.get('header'), str) or type(item.get('multiSelect')) is not bool):
                return None
            options = item.get('options')
            if not isinstance(options, list) or not 2 <= len(options) <= 4 or any(
                    not isinstance(option, dict) or not isinstance(option.get('label'), str) or not option['label']
                    or not isinstance(option.get('description'), str) for option in options):
                return None
            if any(q['question'] == item['question'] for q in clean):
                return None
            clean.append({key: item[key] for key in ('question', 'header', 'multiSelect')})
            clean[-1]['options'] = [{key: option[key] for key in ('label', 'description')} for option in options]
        block = {'type': 'tool_use', 'id': 'toolu_'+uuid.uuid4().hex, 'name': 'AskUserQuestion',
                 'input': {'questions': clean}}
        if len(json.dumps(block, ensure_ascii=False).encode()) > 24000:
            return None
        return cls(session, request_id, original, block)

    def decision(self, result):
        decision = {'type': 'chat.permission-response', 'requestId': self.request_id, 'allow': False}
        if result.get('is_error') is True:
            return {**decision, 'message': 'The user declined or dismissed the request in Claude Desktop.'}
        content = result.get('content')
        if isinstance(content, list) and all(isinstance(item, dict) and item.get('type') == 'text'
                                              and isinstance(item.get('text'), str) for item in content):
            content = ''.join(item['text'] for item in content)
        if not isinstance(content, str) or len(content) > 32000:
            raise CloudCliError('Claude Desktop did not return a supported answer', 400)
        if self.plan is not None:
            approved = 'User has approved exiting plan mode. You can now proceed.'
            if content.startswith(approved) and self.only_context(content[len(approved):]):
                return {**decision, 'allow': True, 'updatedInput': self.original}
            if not content.startswith('User has approved your plan. You can now start coding.'):
                raise CloudCliError('Claude Desktop did not return an explicit plan approval', 400)
            _, found, local_plan = content.partition('\n\n## Approved Plan:\n')
            if found:
                local_plan = re.split(r'\n\n<system-reminder>\s*## Exited Plan Mode\b', local_plan, maxsplit=1)[0]
            if not found or local_plan.strip() != self.plan.strip():
                raise CloudCliError('The plan approved in Desktop differs from the CloudCLI plan', 409)
            return {**decision, 'allow': True, 'updatedInput': self.original}
        # Claude Code returns a rendered answer map, optionally followed by context reminders.
        prefix = 'Your questions have been answered: '
        suffix = '. You can now continue with these answers in mind.'
        end = content.rfind(suffix)
        tail = content[end+len(suffix):]
        if not content.startswith(prefix) or end < len(prefix) or not self.only_context(tail):
            raise CloudCliError('Claude Desktop did not return a supported answer', 400)
        body = content[len(prefix):end]
        questions = [item['question'] for item in self.block['input']['questions']]
        names = '|'.join(re.escape(question) for question in questions)
        pattern = f'(?:^|", )"({names})"="'
        matches = list(re.finditer(pattern, body))
        if (not matches or matches[0].start() != 0 or not body.endswith('"')
                or {match[1] for match in matches} != set(questions) or len(matches) != len(questions)):
            raise CloudCliError('Claude Desktop answer does not match the original questions', 400)
        answers = {match[1]: body[match.end():matches[index+1].start() if index+1 < len(matches) else -1]
                   for index, match in enumerate(matches)}
        if any(not answer.strip() or len(answer) > 4096 for answer in answers.values()):
            raise CloudCliError('Claude Desktop returned an empty or oversized answer', 400)
        if self.upstream_tool is not None:
            selected = answers[questions[0]]
            if selected == '允许一次':
                return {**decision, 'allow': True, 'updatedInput': self.original}
            if selected == '拒绝':
                return {**decision, 'message': 'The user denied this tool request in Claude Desktop.'}
            raise CloudCliError('Choose Allow once or Deny for this CloudCLI tool request', 400)
        return {**decision, 'allow': True, 'updatedInput': {**self.original, 'answers': answers}}

    @staticmethod
    def only_context(text):
        return not re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.S).strip()


@dataclass
class CloudCliPausedTurn:
    events: object
    progress: object
    question: CloudCliApproval
    previous_history: str
    project: str
    workspace: Optional[str]
    request: str
    reply: dict
    expected: str
    timer: Optional[threading.Timer] = None


SSH_TRANSPORT_ERROR = re.compile(
    r'^(?:Connection timed out during banner exchange\b'
    r'|ssh: connect to host [^\n]+ port \d+: Connection (?:timed out|refused)\b'
    r'|kex_exchange_identification: read: Connection reset by peer\b'
    r'|Connection (?:reset|closed) by [^\n]+ port \d+\b'
    r'|client_loop: send disconnect: Broken pipe\b)', re.M)


class CloudCliProgress:
    """Send assistant text and native thinking, with brief summaries for remote activity."""

    def __init__(self, stream, web_origin, native_tools=(), model=None, tool_approvals=False, device=None):
        self.stream = stream
        self.web_origin = web_origin
        self.native_tools = native_tools
        self.tool_approvals = tool_approvals
        self.plan_candidate = None
        self.deferred_questions = []
        self.awaiting_decision = None
        self.on_session = None
        self.on_disconnect = None
        self.stop_requested = threading.Event()
        self.client_detached = threading.Event()
        self.output_closed = False
        self.stopping = threading.Event()
        self.dispatched = False
        self.dispatch_ms = None
        self.forwarded_text = None
        self.workspace_prompt_fingerprint = None
        self.turn_position = None
        self.edit_anchor = None
        self.session = None
        self.parts = []
        self.reply_bytes = 0
        self.pending = ''
        self.sent = 0
        self.has_assistant_text = False
        self.notice_chars = {False: 0, True: 0}
        self.thinking_chars = 0
        self.tools = {}
        self.tool_failures = set()
        self.device_command = shlex.split(device['command']) if device else ()
        self.device_name = 'Windows' if device and device['platform'] == 'windows' else '本机设备'
        self.device_tools = {}
        self.device_failure = False
        self.device_retry_reported = False
        self.tasks = {}
        self.activity_reported = False
        self.permissions = {}
        self.questions = {}
        self.unrecognized = set()
        self.context_budget = None
        self.account_footer = None
        self.model = model
        self.reported_model = None
        self.other_models = set()

    def write(self, text, kind='text'):
        if not text or self.output_closed:
            return
        self.reply_bytes += len(text.encode('utf-8'))
        if self.reply_bytes > MAX_CLOUDCLI_REPLY:
            raise CloudCliError('CloudCLI reply exceeds the gateway response size limit')
        if self.stream is not None and not self.client_detached.is_set():
            try:
                if kind == 'thinking':
                    self.stream.thinking(text)
                else:
                    self.stream.text(text)
            except ConnectionAbortedError:
                if self.on_disconnect is None:
                    raise
                self.on_disconnect(self.stream)
                if self.output_closed:
                    return
        if self.parts and self.parts[-1]['type'] == kind:
            self.parts[-1][kind] += text
        else:
            block = {'type': kind, kind: text}
            if kind == 'thinking':
                block['signature'] = ''
            self.parts.append(block)

    def notice(self, title, output=None, *, action=None):
        body = '\n\n> **SafeCode** · '+title
        if isinstance(output, str) and output.strip():
            preview = ''.join(c if c.isprintable() or c == '\n' else ' ' for c in output[:6000])
            # Indented quoted code keeps Markdown and HTML from tool output inert.
            body += '\n>\n'+'\n'.join('>     '+line for line in preview.split('\n')[:120])
            if len(output) > 6000 or preview.count('\n') >= 120:
                body += '\n>     …'
        if action:
            body += '\n>\n> '+action
        interactive = action is not None
        limit = 32000 if interactive else 64000
        used = self.notice_chars[interactive]
        if used + len(body) > limit:
            if used <= limit:
                self.notice_chars[interactive] = limit + 1
                self.write('\n\n> **SafeCode** · 后续活动超出显示上限，可在 CloudCLI 查看。\n\n')
            return
        self.notice_chars[interactive] += len(body)
        self.write(body+'\n\n')

    @staticmethod
    def fields(data, keys):
        if not isinstance(data, dict):
            return ''
        return '\n'.join(f'{label}: {data[key]}' for key, label in keys.items()
                         if type(data.get(key)) in (str, int) and data[key] != '')

    @staticmethod
    def tool_name(name):
        return name if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', name) else 'CloudCLI'

    def open_session(self):
        if self.web_origin and self.session:
            return f'[打开 CloudCLI 原会话]({self.web_origin}/session/{self.session})'
        return f'打开 CloudCLI 会话 `{self.session}`' if self.session else '打开 CloudCLI 原会话'

    def question(self, data, source, action=None):
        questions = data.get('questions') if isinstance(data, dict) else None
        if not isinstance(questions, list):
            return
        details = []
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get('question'), str):
                continue
            mode = '多选' if question.get('multiSelect') is True else '单选'
            header = question.get('header')
            heading = self.preview(header, 120) if isinstance(header, str) else ''
            lines = [f'{heading}（{mode}）', self.preview(question['question'], 1600)]
            options = question.get('options')
            if isinstance(options, list):
                for index, option in enumerate(options, 1):
                    if not isinstance(option, dict) or not isinstance(option.get('label'), str):
                        continue
                    label, description = option['label'], option.get('description')
                    line = f'{index}. {self.preview(label, 240)}'
                    if isinstance(description, str) and description:
                        line += ' — '+self.preview(description, 640)
                    lines.append(line)
            lines.append('其他：可在 CloudCLI 输入自定义答案。')
            details.append('\n'.join(lines))
        if not details:
            return
        fingerprint = hashlib.sha256('\n'.join(details).encode()).digest()
        if self.questions.get(fingerprint, source) != source:
            # The same question may arrive as both a tool call and an approval.
            del self.questions[fingerprint]
            return
        self.questions[fingerprint] = source
        for output in details:
            self.notice('Claude 提出了选择题', output,
                        action=action or self.open_session()+'查看题目并处理待答选项。')

    @staticmethod
    def preview(text, length):
        return text if len(text) <= length else text[:length-1]+'…'

    def permission(self, event):
        kind, request = event['kind'], event.get('requestId')
        if not isinstance(request, str) or not request:
            return
        if kind == 'permission_request':
            if request in self.permissions:
                return
            tool = self.tool_name(event.get('toolName'))
            self.permissions[request] = tool
            data = event.get('input')
            if tool == 'AskUserQuestion':
                self.question(data, kind)
            detail = self.fields(data, {'description': '说明', 'command': '命令', 'plan': '计划',
                                        'file_path': '文件', 'message': '消息', 'url': '网址'})
            waiting = '等待回答' if tool == 'AskUserQuestion' else '等待授权'
            self.notice(f'`{tool}` {waiting}', detail, action=self.open_session()+'处理此请求。')
            return
        tool = self.permissions.pop(request, 'CloudCLI')
        title = '已收到处理结果' if kind == 'permission_resolved' else '等待已取消'
        self.notice(f'`{tool}` {title}', event.get('reason'))

    def task(self, event):
        status = event.get('status') or event.get('event')
        task = event.get('taskId')
        self.tasks[task if isinstance(task, str) else None] = status
        self.activity()

    def activity(self):
        if not self.activity_reported:
            self.activity_reported = True
            self.notice('正在执行工具操作')

    def summarize_activity(self):
        if not self.activity_reported:
            return
        summary = []
        if self.tools:
            summary.append(f'工具调用 {len(self.tools)} 次')
        if self.tasks:
            summary.append(f'后台任务 {len(self.tasks)} 项')
        failed = len(self.tool_failures) + sum(status == 'failed' for status in self.tasks.values())
        if failed:
            summary.append(f'其中 {failed} 项操作未成功')
        summary.append(self.open_session()+'查看过程详情')
        self.notice(' · '.join(summary))

    def track_device_command(self, identifier, data):
        command = data.get('command') if isinstance(data, dict) else None
        if not self.device_command or not isinstance(command, str):
            return
        try:
            args = shlex.split(command)
            config = args.index('-F')
        except ValueError:
            return
        if (args[0] != 'ssh' or args[config+1:config+2] != self.device_command[2:3]
                or self.device_command[-1] not in args[config+2:]):
            return
        self.device_tools[identifier] = self.device_failure
        if self.device_failure and not self.device_retry_reported:
            self.device_retry_reported = True
            self.notice(f'正在再次尝试连接 {self.device_name}')

    def tool_result(self, event):
        identifier = event.get('toolId')
        if not isinstance(identifier, str):
            return
        self.tools.setdefault(identifier, 'CloudCLI')
        result = event.get('toolResult')
        nested = result if isinstance(result, dict) else {}
        failed = any(item.get(flag) is True for item in (event, nested) for flag in ('isError', 'is_error'))
        output = event.get('content')
        retry = self.device_tools.pop(identifier, None)
        if retry is not None and isinstance(output, str):
            match = SSH_TRANSPORT_ERROR.search(output[:16384])
            if match:
                failed = True
                if not self.device_failure:
                    detail = '连接超时' if 'timed out' in match[0] else '连接中断'
                    self.notice(f'{self.device_name} SSH {detail}，本次命令未完成')
                self.device_failure = True
            elif retry and self.device_failure and not failed and output.strip():
                self.device_failure = self.device_retry_reported = False
                self.notice(f'{self.device_name} 的后续命令已返回结果')
        if failed:
            self.tool_failures.add(identifier)

    def incomplete(self, message, status=502):
        if self.device_failure:
            return CloudCliError(f'{self.device_name} 连接中断，本轮未完成。连接恢复后可在原对话重试。', 503)
        return CloudCliError(message, status)

    def report_thinking(self, content):
        remaining = 64000 - self.thinking_chars
        if remaining <= 0 or not isinstance(content, str) or not content:
            return
        if not content.strip() and (not self.parts or self.parts[-1]['type'] != 'thinking'):
            return
        # CloudCLI exposes a display excerpt, without a provider signature.
        preview = ''.join(c if c.isprintable() or c in '\n\t' else ' ' for c in content[:remaining])
        self.thinking_chars += len(preview)
        self.write(preview, 'thinking')

    def assistant(self, event, parent):
        value = event.get('content')
        if not isinstance(value, str) or not value:
            return
        if parent:
            return
        reported_model = event.get('model')
        if not (isinstance(reported_model, str) and reported_model.startswith('claude-')
                and MODEL.fullmatch(reported_model)):
            reported_model = None
        if reported_model:
            self.reported_model = reported_model
        requested_model = self.model.removesuffix('[1m]') if self.model else None
        if (reported_model and requested_model and requested_model not in CLOUDCLI_MODEL_SELECTORS
                and reported_model.removesuffix('[1m]') != requested_model
                and not (requested_model in CLAUDE_MODEL_FAMILIES
                         and cloudcli_model_family(reported_model) == requested_model)):
            self.other_models.add(reported_model)
        if event.get('compact') or event.get('isCompactSummary') or event.get('isLocalCommandStdout'):
            title = '上下文压缩摘要' if event.get('isCompactSummary') else (
                '本地命令输出' if event.get('isLocalCommandStdout') else '上下文压缩')
            self.notice(title, value)
        elif self.pending.startswith(value):
            self.write(value[self.sent:])
            self.pending = self.pending[len(value):]
            self.sent = max(0, self.sent-len(value))
            self.has_assistant_text |= bool(value.strip())
        else:
            self.write(value[self.sent:] if value.startswith(self.pending) else value)
            self.pending = ''
            self.sent = 0
            self.has_assistant_text |= bool(value.strip())

    def feed(self, event):
        kind = event.get('kind')
        parent = event.get('parentToolUseId')
        parent = parent if isinstance(parent, str) else None
        if kind == 'stream_delta':
            delta = event.get('content')
            if not isinstance(delta, str) or not delta:
                return
            if parent:
                return
            if len(self.pending) + len(delta) > MAX_CLOUDCLI_REPLY:
                raise CloudCliError('CloudCLI reply exceeds the gateway response size limit')
            self.pending += delta
            # A provider can surface an error as text before it identifies the error.
            prefixes = ('API Error:', 'Not logged in')
            if not self.sent and any(p.startswith(self.pending) or self.pending.startswith(p) for p in prefixes):
                return
            self.write(self.pending[self.sent:])
            self.sent = len(self.pending)
        elif kind == 'text':
            if event.get('role') == 'assistant':
                self.assistant(event, parent)
            elif event.get('isLocalCommand') is True:
                self.notice('本地命令', event.get('displayText') or event.get('content'))
        elif kind == 'thinking':
            if not parent:
                self.report_thinking(event.get('content'))
        elif kind == 'tool_use':
            tool, identifier = self.tool_name(event.get('toolName')), event.get('toolId')
            if not isinstance(identifier, str) or identifier in self.tools:
                return
            self.tools[identifier] = tool
            data = event.get('toolInput')
            if tool == 'Bash':
                self.track_device_command(identifier, data)
            if tool == 'AskUserQuestion':
                if tool in self.native_tools:
                    self.deferred_questions.append(data)
                else:
                    self.question(data, kind)
            else:
                if tool == 'ExitPlanMode' and isinstance(data, dict) and isinstance(data.get('plan'), str):
                    self.plan_candidate = data
                self.activity()
        elif kind == 'tool_result':
            self.tool_result(event)
            self.activity()
        elif kind in ('permission_request', 'permission_resolved', 'permission_cancelled'):
            self.permission(event)
        elif kind in ('task_status', 'task_notification'):
            self.task(event)
        elif kind == 'history_truncated':
            title = '已回到你编辑的位置，正在重新执行' if self.edit_anchor is not None else 'CloudCLI 调整了本轮上下文'
            self.notice(title, event.get('summary') or event.get('content'))
        elif kind == 'status':
            if event.get('text') == 'token_budget':
                self.context_budget = event.get('tokenBudget')
            else:
                self.notice('CloudCLI 状态', event.get('text') or event.get('content') or event.get('status'))
        elif isinstance(kind, str) and kind not in ('complete', 'session_created', 'stream_delta', 'stream_end'):
            if kind not in self.unrecognized:
                self.unrecognized.add(kind)
                self.notice('CloudCLI 返回了当前版本未识别的事件', kind)

    def finish(self, usage=None):
        for data in self.deferred_questions:
            self.question(data, 'tool_use', action='可以在当前 Desktop 对话中回复。')
        self.summarize_activity()
        if self.other_models:
            self.notice('CloudCLI 报告本轮使用了其他模型', '\n'.join(sorted(self.other_models)))
        if isinstance(self.context_budget, dict):
            used, total = self.context_budget.get('used'), self.context_budget.get('total')
            if type(used) is int and type(total) is int and used >= 0 and total > 0:
                self.notice(f'上下文用量 {used:,} / {total:,} tokens')
        if self.permissions:
            self.notice('CloudCLI 已结束本轮，仍有请求未报告处理结果',
                        action=self.open_session()+'查看状态。')
        if not self.has_assistant_text and not self.sent:
            self.notice('CloudCLI 已完成本轮，未返回额外的助手文字')
        if self.account_footer is not None and usage is not None:
            for line in self.account_footer(self.reported_model or self.model, usage):
                self.notice(line)
        return self.take_content()

    def take_content(self):
        content, self.parts = self.parts, []
        self.reply_bytes = 0
        return content

    def show_plan(self, approval):
        text = approval.plan
        prompts = approval.original.get('allowedPrompts')
        if isinstance(prompts, list) and prompts:
            text += '\n\nCloudCLI 提交的工具权限说明\n'+json.dumps(prompts, ensure_ascii=False, indent=2)
        text = ''.join(c if c in '\r\n\t' or c.isprintable() else ' ' for c in text)
        self.write('\n\n> **SafeCode** · 请先阅读云端计划，再在 Desktop 确认。\n>\n'
                   +'\n'.join('>     '+row for row in text.splitlines())+'\n\n')

    def show_tool_approval(self, approval):
        inputs = json.dumps(approval.original, ensure_ascii=False, indent=2)
        inputs = ''.join(c if c.isprintable() or c == '\n' else ' ' for c in inputs)
        self.write(f'\n\n> **SafeCode** · CloudCLI 等待授权 `{approval.tool_name}`\n>\n'
                   +'\n'.join('>     '+row for row in inputs.splitlines())+'\n\n')


def cloudcli_events(response, progress, expected_session=None, *, transport_socket=None):
    """Consume CloudCLI's one-line data events and require successful termination."""
    lines = total = 0
    usage = dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0)
    complete = False
    possible_error = None
    # Bound an unacknowledged dispatch. After a session is confirmed, the
    # provider's completion, cancellation or transport failure ends the turn.
    deadline = time.monotonic() + CLOUDCLI_START_TIMEOUT
    startup_error = 'CloudCLI did not confirm a session before the gateway timed out'
    while True:
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise CloudCliError(startup_error, 504)
        try:
            if isinstance(response, CloudCliSocketEvents):
                line = response.readline(MAX_REQUEST + 1, deadline)
            elif transport_socket is not None and response.isclosed():
                line = b''
            else:
                if transport_socket is not None:
                    transport_socket.settimeout(remaining)
                line = response.readline(MAX_REQUEST + 1)
        except TimeoutError:
            if deadline is None:
                raise CloudCliError('CloudCLI transport timed out while reading an active session', 504) from None
            raise CloudCliError(startup_error, 504) from None
        if deadline is not None and time.monotonic() >= deadline:
            raise CloudCliError(startup_error, 504)
        lines += 1
        total += len(line)
        diagnostic(streamLines=lines, receivedBytes=total)
        if not line:
            break
        if len(line) > MAX_REQUEST:
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
        if kind == 'error' or event_type == 'error' or (kind != 'tool_result' and event.get('is_error') is True):
            raise cloudcli_failure(event, progress)
        if event_type == 'status' and event.get('message') not in ('Session started', 'Repository cloned and session started'):
            progress.notice('CloudCLI 状态', event.get('message'))
        if event_type == 'session-id':
            candidate = event.get('sessionId')
            if not isinstance(candidate, str) or not CLOUDCLI_SESSION_ID.fullmatch(candidate):
                raise CloudCliError('CloudCLI returned an invalid session identifier')
            if expected_session is not None and expected_session != candidate:
                raise CloudCliError('CloudCLI unexpectedly changed the resumed session')
            if progress.session is not None and progress.session != candidate:
                raise CloudCliError('CloudCLI changed the run session identifier')
            opened = progress.session is None
            progress.session = candidate
            deadline = None
            if opened:
                if progress.on_session:
                    progress.on_session(candidate)
                if progress.stream is not None and getattr(progress.stream, 'send_events', True):
                    progress.notice('已接收任务')
        if kind == 'text' and event.get('role') == 'assistant' and not event.get('parentToolUseId'):
            content = event.get('content')
            if event.get('isApiErrorMessage') is True or event.get('is_api_error_message') is True:
                raise cloudcli_failure(event, progress)
            if possible_error is not None and content != possible_error['content']:
                progress.feed(possible_error)
                possible_error = None
            if isinstance(content, str) and (re.match(r'^API Error:\s*[45]\d\d\b', content)
                    or content.startswith('Not logged in') and '/login' in content):
                # A successfully generated log example remains ordinary output.
                possible_error = event
                continue
        if possible_error is not None and kind == 'stream_delta' and not event.get('parentToolUseId'):
            progress.feed(possible_error)
            possible_error = None
        if kind == 'status' and event.get('text') == 'token_budget':
            budget = event.get('tokenBudget')
            if isinstance(budget, dict):
                usage.update({dst: budget[src] for dst, src in CLOUDCLI_USAGE_FIELDS.items()
                              if type(budget.get(src)) is int and budget[src] >= 0})
        if kind == 'complete':
            if event.get('aborted') is True:
                if progress.stopping.is_set():
                    raise CloudCliStopped('Desktop stopped its CloudCLI turn')
                if progress.session and progress.dispatched:
                    raise CloudCliNativeStop('本轮已在 CloudCLI 中停止。请在原对话发送下一条消息。', 400)
                raise progress.incomplete('CloudCLI run was canceled', 409)
            if type(event.get('exitCode')) is not int or event['exitCode'] != 0 or event.get('success') is False:
                raise cloudcli_failure(possible_error or event, progress)
            complete = True
        if event_type == 'done':
            if progress.session and complete:
                if progress.awaiting_decision:
                    raise CloudCliError('CloudCLI did not confirm the Desktop answer', 409)
                if possible_error is not None:
                    progress.feed(possible_error)
                return progress.session, progress.finish(usage), usage
            break
        if progress.awaiting_decision and event.get('requestId') == progress.awaiting_decision:
            if kind == 'permission_cancelled':
                raise CloudCliError('The CloudCLI question was cancelled before the Desktop answer was accepted', 409)
            if kind == 'permission_resolved':
                progress.awaiting_decision = None
        if (progress.native_tools and not progress.output_closed and not progress.awaiting_decision
                and not complete and kind == 'permission_request'):
            question = CloudCliApproval.read(event, progress.session, progress.native_tools,
                                             progress.plan_candidate, progress.tool_approvals)
            if event.get('toolName') == 'ExitPlanMode':
                progress.plan_candidate = None
            if question is not None:
                progress.deferred_questions = [data for data in progress.deferred_questions if data != question.original]
                progress.permissions[question.request_id] = question.tool_name
                if question.plan is not None:
                    progress.show_plan(question)
                if question.upstream_tool is not None:
                    progress.show_tool_approval(question)
                if progress.output_closed:
                    continue
                with response.keepalive() if isinstance(response, CloudCliSocketEvents) else nullcontext():
                    yield question
                continue
        progress.feed(event)
    if possible_error is not None:
        raise cloudcli_failure(possible_error, progress)
    raise progress.incomplete('CloudCLI ended without a confirmed assistant reply')


class CloudCliSocketEvents:
    """Expose the chat WebSocket's normalized turn events to the Messages adapter."""

    PAUSED_HEARTBEAT_SECONDS = 10

    def __init__(self, connection, session, review_deadline=None):
        self.connection = connection
        self.session = session
        self.review_deadline = review_deadline
        self.started = False
        self.complete = False
        self.first_frame = None

    @contextmanager
    def keepalive(self):
        # CloudCLI checks for pong every 30 seconds. While the HTTP turn waits
        # for a Desktop answer, no recv() is active to reply to its ping.
        finished = threading.Event()

        def send_pongs():
            _, websocket_error = cloudcli_websocket()
            while not finished.wait(self.PAUSED_HEARTBEAT_SECONDS):
                try:
                    self.connection.pong()
                except (OSError, websocket_error):
                    return

        worker = threading.Thread(target=send_pongs, daemon=True)
        worker.start()
        try:
            yield
        finally:
            finished.set()
            worker.join(timeout=1)

    def readline(self, limit, deadline):
        if not self.started:
            self.started = True
            self.first_frame = self.read_event(limit, deadline)
            if not self.first_frame:
                return b''
            return b'data: '+json.dumps({'type': 'session-id', 'sessionId': self.session}).encode()
        if self.first_frame is not None:
            event, self.first_frame = self.first_frame, None
            return event
        if self.complete:
            return b'data: {"type":"done"}'
        return self.read_event(limit, deadline)

    def read_event(self, limit, deadline):
        from websocket import ABNF, WebSocketTimeoutException
        effective_deadline = deadline
        if self.review_deadline is not None and (deadline is None or self.review_deadline <= deadline):
            effective_deadline = self.review_deadline
        while True:
            remaining = effective_deadline - time.monotonic() if effective_deadline is not None else None
            try:
                if remaining is not None and remaining <= 0:
                    raise WebSocketTimeoutException()
                self.connection.settimeout(remaining)
                # Control traffic is not a confirmation of a new turn.
                opcode, line = self.connection.recv_data(control_frame=True)
            except WebSocketTimeoutException:
                if self.review_deadline is not None and effective_deadline == self.review_deadline:
                    raise CloudCliError('CloudCLI prompt review timed out', 504) from None
                raise TimeoutError() from None
            if opcode == ABNF.OPCODE_CLOSE:
                return b''
            if opcode in (ABNF.OPCODE_PING, ABNF.OPCODE_PONG):
                continue
            if not line:
                return b''
            if len(line) > limit:
                raise CloudCliError('CloudCLI response exceeds the gateway limit')
            event = json.loads(line)
            if not isinstance(event, dict):
                raise CloudCliError('CloudCLI returned an invalid chat event')
            kind, session = event.get('kind'), event.get('sessionId')
            if session and session != self.session:
                continue
            if self.review_deadline is not None and session == self.session and kind in (
                    'tool_use', 'tool_result', 'permission_request', 'task_started'):
                raise CloudCliError('CloudCLI prompt review attempted to use a tool')
            if kind == 'protocol_error':
                code = event.get('code')
                if code == 'RUN_IN_PROGRESS':
                    raise CloudCliEditRefused('This CloudCLI session already has an active run', 409)
                if code in ('ANCHOR_NOT_FOUND', 'ANCHOR_REQUIRED', 'ANCHOR_LOOKUP_FAILED', 'EDIT_NOT_SUPPORTED'):
                    raise CloudCliEditRefused('CloudCLI could not verify the message being edited', 409)
                raise CloudCliError('CloudCLI did not accept the Desktop chat message', 400)
            if session == self.session and kind not in ('session_upserted', 'chat_subscribed', 'side_question',
                                                        'scheduled_messages_changed', 'loading_progress'):
                self.complete = kind == 'complete'
                return b'data: '+(line.encode() if isinstance(line, str) else line)


class CloudCliStopWatch:
    def __init__(self, backend, progress, dispatch_ms):
        self.backend = backend
        self.progress = progress
        self.dispatch_ms = dispatch_ms
        self.can_abort = bool(backend.config.get('desktopQuestions'))
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.session = None
        self.opened_ms = None
        self.started_at = None
        self.attempted = False

    def start(self, session):
        self.session = session
        self.opened_ms = int(time.time()*1000)
        threading.Thread(target=self.watch, daemon=True).start()

    def inactive(self, stream):
        return self.finished.is_set() or self.progress.stream is not stream

    def find_run(self):
        started = self.backend.running_since(self.session)
        return started if started is not None and self.dispatch_ms <= started <= self.opened_ms else None

    def watch(self):
        if self.can_abort:
            try:
                self.started_at = self.find_run()
            except CloudCliError:
                pass
        while not self.finished.wait(.25):
            stream = self.progress.stream
            if stream is None:
                continue
            if self.progress.stop_requested.is_set():
                self.stop(stream)
                return
            if not self.progress.client_detached.is_set() and (stream.disconnected()
                    or not stream.ping_if_idle(self.finished)):
                self.detach(stream)

    def detach(self, stream):
        if not self.inactive(stream):
            self.progress.client_detached.set()

    def stop(self, stream):
        with self.lock:
            if self.attempted or self.session is None or self.inactive(stream):
                return
            self.attempted = True
            if not self.can_abort:
                # The Agent API has no cancel operation. Keep reading this response
                # so its owner can recover the result without dispatching it again.
                return
        try:
            connect, websocket_error = cloudcli_websocket()
        except CloudCliError:
            return
        try:
            # A replacement run created from the CloudCLI UI must never be stopped.
            started = self.started_at if self.started_at is not None else self.find_run()
            if started is None:
                return
            with closing(self.backend.web_socket(connect)) as ws:
                if self.inactive(stream) or self.backend.running_since(self.session) != started:
                    return
                if self.inactive(stream):
                    return
                self.progress.stopping.set()
                ws.send(json.dumps({'type': 'chat.abort', 'sessionId': self.session}))
                self.progress.output_closed = True
        except (OSError, CloudCliError, websocket_error):
            return


_native_catalog_lock = threading.Lock()
_native_catalog_options = {}
_native_catalog_expires = 0.0


def native_cloudcli_options():
    """Read the work account's model choices without making a model request."""
    global _native_catalog_options, _native_catalog_expires
    with _native_catalog_lock:
        if time.monotonic() >= _native_catalog_expires:
            try:
                catalog = native_model_catalog(timeout=5, include_options=True)
                options = dict(catalog.get('options', {}))
                for row in catalog['data']:
                    model = row['id']
                    option = {'label': row.get('display_name'), 'description': row.get('description'),
                              'effort': {'values': [{'value': level} for level in row.get('supported_effort_levels', [])]}}
                    options.setdefault(model, option)
                    if row.get('supports_1m'):
                        options.setdefault(model+'[1m]', option)
                _native_catalog_options = options
                retry = 600 if options else 60
            except (GatewayError, OSError, ValueError, TypeError, AttributeError, KeyError):
                retry = 60
            _native_catalog_expires = time.monotonic()+retry
        return _native_catalog_options


class ActiveCloudCliTurn:
    def __init__(self, history):
        self.history = history
        self.client_detached = threading.Event()
        self.stop_requested = threading.Event()
        self.finished = threading.Event()


class CloudCliBackend:
    project_lock = threading.Lock()

    def __init__(self, directory, config, device=None):
        self.config = config
        self.device = device
        self.parallel = False
        self.active_conversations = {}
        self.active_lock = threading.Lock()
        self.paused_turns = {}
        self.approval_lock = threading.Lock()
        self.approval_token = None
        self.approval_expires = 0
        self.model_options_lock = threading.Lock()
        self.model_options = ({}, False)
        self.automatic_models = (
            config['hostControl'] is None and tuple(config['models']) == DEFAULT_CLOUDCLI_MODELS
            and Path(config['projectPath']) == (Path.home()/DEFAULT_CLOUDCLI_PROJECT).resolve())
        self.model_options_expires = 0
        self.path = directory/'cloudcli-history.sqlite3'
        # Display and capability changes preserve existing session bindings.
        identity = {k: v for k, v in config.items() if k not in ('webOrigin', 'desktopQuestions',
                                                                  'extendedContextModels', 'desktopToolApprovals')}
        identity['apiKey'] = hashlib.sha256(config['apiKey'].encode()).hexdigest()
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
            db.execute('CREATE TABLE IF NOT EXISTS compact_receipts (scope TEXT PRIMARY KEY, history TEXT NOT NULL, '
                       'session TEXT NOT NULL, marker TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS request_operations (identity TEXT PRIMARY KEY, '
                       'operation TEXT NOT NULL, pending INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS request_operations_pending ON request_operations(operation) WHERE pending=1')
            # A receipt identifies an issued card. It never authorizes a tool decision.
            db.execute('CREATE TABLE IF NOT EXISTS question_receipts (scope TEXT PRIMARY KEY, history TEXT NOT NULL, '
                       'card TEXT NOT NULL, session TEXT NOT NULL, model TEXT NOT NULL, project TEXT NOT NULL, '
                       'workspace TEXT, created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS session_state (scope TEXT NOT NULL, session TEXT NOT NULL, '
                       'model TEXT NOT NULL, tip_anchor TEXT, updated REAL NOT NULL, PRIMARY KEY (scope, session))')
            db.execute('CREATE TABLE IF NOT EXISTS edit_turns (scope TEXT NOT NULL, session TEXT NOT NULL, '
                       'prefix TEXT NOT NULL, position INTEGER NOT NULL, anchor TEXT NOT NULL, '
                       'updated REAL NOT NULL, PRIMARY KEY (scope, session, prefix))')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_prompts (scope TEXT NOT NULL, session TEXT NOT NULL, '
                       'position INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY (scope, session))')
            db.execute('CREATE TABLE IF NOT EXISTS stopped_before_anchor (scope TEXT NOT NULL, session TEXT NOT NULL, '
                       'prefix TEXT NOT NULL, prior_anchor TEXT, PRIMARY KEY (scope,session))')
            db.execute('CREATE TABLE IF NOT EXISTS anchor_receipts (scope TEXT NOT NULL, session TEXT NOT NULL, '
                       'prefix TEXT NOT NULL, position INTEGER NOT NULL, fingerprint TEXT NOT NULL, '
                       'dispatched INTEGER NOT NULL, ended INTEGER NOT NULL, PRIMARY KEY (scope,session))')

    def verified_model_options(self):
        if not self.config.get('desktopQuestions'):
            return {}, False
        with self.model_options_lock:
            if time.monotonic() < self.model_options_expires:
                return self.model_options
            try:
                body = self.fetch_model_options()
                listing = body['data']['models']
                options = {entry['value']: entry for entry in listing['OPTIONS']
                           if isinstance(entry, dict) and isinstance(entry.get('value'), str)
                           and CLOUDCLI_MODEL_ID.fullmatch(entry['value'])}
                self.model_options = options, listing.get('CUSTOM_MODELS_ONLY') is True
                self.model_options_expires = time.monotonic()+600
            except (CloudCliError, OSError, HTTPException, ValueError, TypeError, KeyError, RecursionError):
                self.model_options_expires = time.monotonic()+60
            return self.model_options

    def fetch_model_options(self):
        for attempt in range(2):
            token = self.question_token()
            with closing(HTTPConnection('127.0.0.1', self.config['port'], timeout=5)) as conn:
                conn.request('GET', '/api/providers/claude/models', headers={'Authorization': 'Bearer '+token})
                with conn.getresponse() as response:
                    if response.status == 200:
                        return json.loads(response.read(524288))
                    if response.status != 401 or attempt != 0:
                        break
            with self.approval_lock:
                if self.approval_token == token:
                    self.approval_token = None
        raise ValueError('CloudCLI model catalog is unavailable')

    def model_choices(self):
        provider, custom_only = self.verified_model_options()
        if not self.automatic_models:
            choices = {model: provider.get(model, {}) for model in self.config['models']}
        elif custom_only:
            choices = dict(provider)
        else:
            native = native_cloudcli_options()
            # CloudCLI supplies the current labels and effort options. Native discovery also supplies
            # concrete IDs and capabilities when the account only has an Agent API Key.
            choices = {model: {**native.get(model, {}), **option} for model, option in provider.items()}
            for model, option in native.items():
                choices.setdefault(model, option)
            if not choices:
                choices = dict.fromkeys(self.config['models'], {})
            else:
                # Existing Desktop profiles may still send the old concrete ID when CloudCLI
                # advertises that model family by alias. Keep those IDs usable during refresh.
                for model in self.config['models']:
                    if cloudcli_model_family(model) in provider:
                        choices.setdefault(model, {})
            self.route_resolved_models(choices, native, provider)
        for model in self.config.get('extendedContextModels', []):
            if model in choices:
                choices.setdefault(model+'[1m]', provider.get(model+'[1m]', choices[model]))
        return choices

    @staticmethod
    def route_resolved_models(choices, native, provider):
        # CloudCLI validates effort against its own model IDs. When native discovery
        # proves which concrete version a family alias selects, use the CloudCLI alias
        # for existing Desktop configs that still request that exact version.
        for alias in CLAUDE_MODEL_FAMILIES:
            resolved = native.get(alias, {}).get('behaves_as')
            if alias not in choices or not resolved or resolved == alias:
                continue
            for suffix in ('', '[1m]'):
                current, target = resolved+suffix, alias+suffix
                if current in choices and current not in provider and target in choices:
                    choices[current] = {**choices[current], 'route': target,
                                        'effort': choices[target].get('effort', choices[current].get('effort'))}

    @staticmethod
    def supported_efforts(option):
        effort = option.get('effort')
        levels = effort.get('values') if isinstance(effort, dict) else None
        return [level.get('value') for level in levels if isinstance(level, dict)] if isinstance(levels, list) else []

    def catalog(self, allowed_models=None):
        choices = self.model_choices()
        if allowed_models is not None:
            choices = {model: option for model, option in choices.items()
                       if option.get('route', model) in allowed_models}
        families = set()
        data = []
        for model, option in choices.items():
            base = model.removesuffix('[1m]')
            if model != base and base in choices:
                continue  # Desktop expands supports1m into a context variant in its picker.
            family = cloudcli_model_family(model)
            default = family is not None and family not in families
            families.add(family)
            name = option.get('label')
            name = name if isinstance(name, str) and name.strip() and name.isprintable() else model
            levels = self.supported_efforts(option)
            extended_option = choices.get(model+'[1m]')
            if extended_option is not None and not levels:
                levels = self.supported_efforts(extended_option)
            row = {'id': model, 'type': 'model', 'display_name': name[:160],
                   'anthropic_family_tier': family, 'is_family_default': default,
                   'supported_effort_levels': [level for level in EFFORT_ORDER+('ultracode',) if level in levels],
                   'supports_1m': extended_option is not None or model != base}
            description = option.get('description')
            if isinstance(description, str) and description.isprintable():
                row['description'] = description[:320]
            resolved = option.get('behaves_as')
            if isinstance(resolved, str) and resolved.startswith('claude-') and MODEL.fullmatch(resolved):
                row['behaves_as'] = resolved
            if model != base:
                row['prefer_1m'] = True
            if base in (*CLAUDE_MODEL_FAMILIES, *CLOUDCLI_MODEL_SELECTORS):
                alias = DESKTOP_MODEL_ALIAS_PREFIX+model
                if alias not in choices:
                    row['desktop_model_id'] = alias
            ultracode_id = DESKTOP_ULTRACODE_PREFIX+model
            if 'ultracode' in levels and ultracode_id not in choices:
                row['desktop_ultracode_model_id'] = ultracode_id
                row['ultracode_supports_1m'] = model != base or (extended_option is not None
                    and 'ultracode' in self.supported_efforts(extended_option))
            data.append(row)
        if not data and allowed_models is None:
            raise CloudCliError('The configured CloudCLI provider has no model choices', 503)
        return {'data': data, 'has_more': False, 'first_id': data[0]['id'] if data else None,
                'last_id': data[-1]['id'] if data else None}

    def managed_models(self):
        models = {}
        for choice, option in self.model_choices().items():
            route = option.get('route', choice)
            if route not in models or route == choice:
                label = option.get('label')
                models[route] = {'id': route, 'name': label[:160] if isinstance(label, str) and label.isprintable() else route}
        return list(models.values())

    def resolve_model(self, requested, context_1m=False, effort=None):
        base = requested.removesuffix('[1m]')
        extended = context_1m or base != requested
        if self.automatic_models or base.startswith(DESKTOP_ULTRACODE_PREFIX) or effort == 'ultracode':
            choices = self.model_choices()
        else:
            choices = dict.fromkeys(self.config['models'], {})
            choices.update(dict.fromkeys((model+'[1m]' for model in self.config.get('extendedContextModels', [])), {}))
        if base not in choices and base+'[1m]' not in choices:
            alias = base.removeprefix(DESKTOP_MODEL_ALIAS_PREFIX)
            if (alias != base and alias in (*CLAUDE_MODEL_FAMILIES, *CLOUDCLI_MODEL_SELECTORS)
                    and (alias in choices or alias+'[1m]' in choices)):
                base = alias
            elif base.startswith(DESKTOP_ULTRACODE_PREFIX):
                base = base.removeprefix(DESKTOP_ULTRACODE_PREFIX)
                effort = 'ultracode'
        if base not in choices and base+'[1m]' not in choices:
            if base in CLAUDE_MODEL_FAMILIES:
                base = next((model.removesuffix('[1m]') for model in choices if cloudcli_model_family(model) == base), None)
            if base is None or (base not in choices and base+'[1m]' not in choices):
                raise CloudCliError('The requested model is not configured for the CloudCLI gateway', 404)
        selected = base+'[1m]' if extended or base not in choices else base
        if selected not in choices:
            raise CloudCliError('1M context is not enabled for the requested CloudCLI model', 400)
        target = choices[selected].get('route', selected)
        if effort == 'ultracode' and effort not in self.supported_efforts(choices.get(target, {})):
            raise CloudCliError('CloudCLI does not offer Ultracode for the requested model', 400)
        return target, effort

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
            if self.allowed_workspace(cwd):
                return cwd
            # Desktop can append Environment as a system message while the
            # top-level system blocks contain only the static prompt.
            hints = {path for message in data.get('messages', ())
                     if isinstance(message, dict) and message.get('role') == 'system'
                     if (path := desktop_working_directory({'system': message.get('content')})) is not None
                     and self.allowed_workspace(path)}
            if len(hints) == 1:
                return hints.pop()
            if scope and self.device['platform'] == 'macos':
                try:
                    from host_control_mcp import exchange_ssh, REMOTE_COMMAND, ToolError
                    command = shlex.split(self.device['command'])
                    request = json.dumps({'name': 'desktop_workspace', 'session': scope}).encode()
                    result = exchange_ssh(['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', *command[1:], REMOTE_COMMAND],
                                          request, timeout=7)
                    folder = result.get('workspace')
                    if valid_mac_workspace(folder):
                        return PurePosixPath(folder)
                except (OSError, ValueError, AttributeError, subprocess.SubprocessError, ToolError):
                    pass
            return None
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

    @staticmethod
    def ssh_workspace_instruction(target, system, workspace, command):
        if workspace is None:
            return (f'本机是 {target}（`{system}`）。通过 `{command}` 在本机执行任务。'
                    '请使用用户指定的目录。任务需要目录且路径未知时，再询问用户。')
        return (
            f'本机是 {target}（`{system}`），当前工作目录为 `{workspace}`。'
            '把它当作本次会话的项目根目录，用户说「这里」「这个项目」或「我的电脑」时，就使用这个目录。\n\n'
            f'需要操作项目时，通过 `{command}` 连接本机。每条命令从项目根目录开始，相对路径也以此为准。'
            '通过同一连接读取项目说明、查找和修改文件、运行 Git、构建及测试。项目修改和生成的文件都保存在本机项目目录里。\n\n'
            '如果操作失败，先检查实际错误，在现有权限内能解决就继续。仅在需要用户提供信息或确认时再询问。'
        )

    def host_instruction(self, project, workspace):
        if self.device:
            system = 'Windows' if self.device['platform'] == 'windows' else 'macOS'
            return self.ssh_workspace_instruction(self.device['hostname'], system, workspace, self.device['command'])
        host = self.config['hostControl']
        if host is None:
            return ('No SafeCode client device is authorized for this conversation. '
                    'Native Read, Write, Edit, Glob, Grep, NotebookEdit and Bash tools run on the '
                    'VPS working account, not on the API user\'s computer. If the user asks to '
                    'inspect, clean, or modify their own desktop, downloads, local files, or local '
                    'applications, stop and ask them to authorize client-device control in SafeCode. '
                    'Never substitute a similarly named VPS directory or claim a VPS operation '
                    'touched their computer. Use these VPS tools only for an explicitly VPS-side task.')
        if workspace is None:
            return '所选本机目录超出了已配置的文件访问范围。需要操作项目文件时，请用户确认一个可访问的本机目录。'
        home = Path.home()
        client = host['clientId']
        allow_ssh_shell = host.get('allowSshShell', False)
        ssh_config = home/'.ssh/safe-code-host'/client/'config'
        command = None
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
        target = host['deviceName'] or '用户选定的电脑'
        if command is not None:
            return self.ssh_workspace_instruction(target, 'macOS', workspace, command)
        return (f'本机是 {target}，当前工作目录为 `{workspace}`。'
                '通过 SafeCode 本机文件工具操作这个目录中的文件。')

    def call(self, payload, progress):
        try:
            started = time.monotonic()
            with closing(HTTPConnection('127.0.0.1', self.config['port'], timeout=90)) as conn:
                diagnostic(stage='cloudcli_connect')
                conn.connect()
                progress.dispatched = True
                dispatch_ms = int(time.time()*1000)
                progress.dispatch_ms = dispatch_ms
                conn.request('POST', '/api/agent', json.dumps(payload, ensure_ascii=False).encode(),
                             {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                              'x-api-key': self.config['apiKey']})
                transport_socket = conn.sock
                with conn.getresponse() as response:
                    diagnostic(stage='cloudcli_response', cloudcliStatus=response.status,
                               cloudcliHeadersMs=min(900000, round((time.monotonic()-started)*1000)))
                    if response.status != 200:
                        status = response.status if response.status in CLOUDCLI_ERROR_STATUS else 502
                        error = CloudCliRejected if status in (400, 401, 403, 404, 409, 413, 429) else CloudCliError
                        if status in (401, 403):
                            raise error('CloudCLI rejected the configured gateway credential', 403)
                        raise error(f'CloudCLI Agent API returned HTTP {response.status}', status)
                    if response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'text/event-stream':
                        raise CloudCliError('CloudCLI Agent API returned an unexpected response')
                    with self.observe_turn(progress, dispatch_ms):
                        return (yield from cloudcli_events(response, progress, payload.get('sessionId'),
                                                          transport_socket=transport_socket))
        except ConnectionAbortedError:
            raise
        except TimeoutError:
            raise CloudCliError('CloudCLI did not finish before the gateway timed out', 504) from None
        except (OSError, HTTPException):
            raise CloudCliError('CloudCLI connection ended before a result was confirmed') from None

    def web_request(self, method, path, data=None, content_type='application/json', timeout=12, *,
                    response_limit=512 * 1024):
        try:
            token = self.question_token()
            headers = {'Authorization': 'Bearer '+token}
            if data is not None:
                headers['Content-Type'] = content_type
            with closing(HTTPConnection('127.0.0.1', self.config['port'], timeout=timeout)) as conn:
                conn.request(method, path, data, headers)
                with conn.getresponse() as response:
                    if response.status not in (200, 201):
                        status = response.status if response.status in (400, 404, 409, 413, 429) else 503
                        raise CloudCliError(f'CloudCLI web API returned HTTP {response.status}', status)
                    raw = response.read(response_limit + 1)
                    if len(raw) > response_limit:
                        raise CloudCliWebResponseTooLarge('CloudCLI web response exceeds the gateway limit')
                    return json.loads(raw)
        except (OSError, HTTPException, ValueError, RecursionError):
            raise CloudCliError('CloudCLI did not return a valid web API response', 502) from None

    def history_pages(self, session, timeout):
        if not CLOUDCLI_SESSION_ID.fullmatch(session):
            raise CloudCliError('CloudCLI returned an invalid session identifier')
        # CloudCLI returns the newest page first, ordered oldest-to-newest
        # within each page. A single tool-heavy turn can fill several pages.
        deadline = time.monotonic() + timeout
        total = None
        offset = 0
        page_size = 64
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudCliError('CloudCLI history anchor lookup timed out', 504)
            path = f'/api/providers/sessions/{session}/messages?limit={page_size}&offset={offset}'
            try:
                result = self.web_request('GET', path, timeout=min(5, remaining), response_limit=CLOUDCLI_HISTORY_LIMIT)
            except CloudCliWebResponseTooLarge:
                if page_size == 1:
                    raise
                page_size //= 2
                continue
            data = result.get('data') if isinstance(result, dict) else None
            rows = data.get('messages') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise CloudCliError('CloudCLI did not return conversation anchors')
            if offset and (data.get('offset') != offset or data.get('total') != total):
                raise CloudCliError('CloudCLI history changed while locating conversation anchors', 409)
            total = data.get('total')
            has_more = data.get('hasMore') is True
            yield rows, has_more
            if not has_more:
                return
            if len(rows) != page_size or type(total) is not int or total <= offset + len(rows):
                raise CloudCliError('CloudCLI did not return its history pagination')
            offset += len(rows)

    def history_users(self, session, *, since=None, anchor=None, minimum=1, timeout=12):
        users = []
        for rows, has_more in self.history_pages(session, timeout):
            page_users = [row for row in rows if isinstance(row, dict) and row.get('sessionId') == session
                          and row.get('role') == 'user' and row.get('kind') == 'text']
            users[:0] = page_users
            if (not has_more or
                    anchor is not None and any(row.get('transcriptAnchorId') == anchor for row in users[1:]) or
                    anchor is None and since is None and len(users) >= minimum):
                return users
            if since is not None and any((seen := self.history_timestamp(row)) is not None and seen < since for row in page_users):
                return users

    def history_before_edit(self, session, anchor, timeout):
        history = []
        target = None
        for rows, _ in self.history_pages(session, timeout):
            if not all(isinstance(row, dict) and row.get('sessionId') == session for row in rows):
                raise CloudCliError('CloudCLI did not return verified Desktop history', 409)
            for index, row in enumerate(rows):
                if row.get('transcriptAnchorId') == anchor:
                    if target is not None or row.get('kind') != 'text' or row.get('role') != 'user':
                        raise CloudCliError('CloudCLI did not return a unique Desktop edit anchor', 409)
                    target = row
                    rows = rows[:index]
            if target is not None:
                # Native forks assign new identifiers to copied transcript rows.
                history[:0] = [{k: v for k, v in row.items()
                                if k not in ('id', 'sessionId', 'transcriptAnchorId')} for row in rows]
        if target is None:
            raise CloudCliError('CloudCLI did not return a verified Desktop edit anchor', 409)
        return history, target.get('content')

    @staticmethod
    def history_timestamp(row):
        try:
            seen = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            return round(seen.timestamp()*1000) if seen.tzinfo is not None else None
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            return None

    def resolve_turn_anchor(self, session, fingerprint, dispatched, ended, *, timeout=12):
        try:
            users = self.history_users(session, since=dispatched, timeout=timeout)
        except CloudCliError:
            return None
        matching = {row['transcriptAnchorId'] for row in users
                    if isinstance(row.get('content'),str)
                    and hashlib.sha256(row['content'].encode()).hexdigest() == fingerprint
                    and isinstance(row.get('transcriptAnchorId'),str)
                    and CLOUDCLI_SESSION_ID.fullmatch(row['transcriptAnchorId'])
                    and (seen := self.history_timestamp(row)) is not None and dispatched <= seen <= ended}
        if len(matching) != 1 or users[-1].get('transcriptAnchorId') not in matching:
            return None
        return users[-1]['transcriptAnchorId']

    def turn_anchor(self, progress, session):
        if not self.config.get('desktopQuestions') or not progress.forwarded_text or progress.dispatch_ms is None:
            return None
        fingerprint = hashlib.sha256(progress.forwarded_text.encode()).hexdigest()
        return self.resolve_turn_anchor(session, fingerprint, progress.dispatch_ms, int(time.time()*1000), timeout=2)

    def running_sessions(self):
        result = self.web_request('GET', '/api/providers/sessions/running')
        data = result.get('data') if isinstance(result, dict) else None
        sessions = data.get('sessions') if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            raise CloudCliError('CloudCLI did not return its running sessions')
        return sessions

    def running_since(self, session):
        return next((entry['startedAt'] for entry in self.running_sessions() if isinstance(entry, dict)
                     and entry.get('sessionId') == session and entry.get('provider') == 'claude'
                     and entry.get('background') is not True and entry.get('canInterrupt') is not False
                     and type(entry.get('startedAt')) is int), None)

    @contextmanager
    def observe_turn(self, progress, dispatch_ms):
        if progress.stream is None:
            yield
            return
        observer = CloudCliStopWatch(self, progress, dispatch_ms)
        progress.on_session = observer.start
        progress.on_disconnect = observer.detach
        try:
            yield
        finally:
            observer.finished.set()
            if progress.stream is not None:
                # Let an in-flight ping finish before the response writes its final event.
                with progress.stream.wire_lock:
                    pass
            progress.on_session = None
            progress.on_disconnect = None

    def upload_attachments(self, attachments):
        if not attachments:
            return []
        boundary = uuid.uuid4().hex
        parts = []
        for name, media, data in attachments:
            parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                          f'Content-Type: {media}\r\n\r\n'.encode(), data, b'\r\n'))
        parts.append(f'--{boundary}--\r\n'.encode())
        result = self.web_request('POST', '/api/assets/files', b''.join(parts),
                                  'multipart/form-data; boundary='+boundary)
        records = result.get('attachments') if isinstance(result, dict) else None
        if not isinstance(records, list) or len(records) != len(attachments):
            raise CloudCliError('CloudCLI did not confirm the uploaded attachments')
        descriptors = []
        for record, (name, media, data) in zip(records, attachments):
            path = record.get('path') if isinstance(record, dict) else None
            if (not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                    or '..' in PurePosixPath(path).parts or record.get('size') != len(data)
                    or record.get('mimeType') != media):
                raise CloudCliError('CloudCLI returned an invalid attachment reference')
            descriptors.append({'path': path, 'name': name, 'mimeType': media, 'size': len(data)})
        return descriptors

    def web_socket(self, connect, timeout=6):
        return connect(f"ws://127.0.0.1:{self.config['port']}/ws",
                       header={'Authorization': 'Bearer '+self.question_token()}, timeout=timeout,
                       suppress_origin=True, http_no_proxy=['127.0.0.1'])

    def call_chat(self, payload, progress, attachments, plan_mode, edit=None, *, title_text, review=False, unrestricted=False):
        connect, websocket_error = cloudcli_websocket()
        session = payload.get('sessionId')
        try:
            diagnostic(stage='cloudcli_connect')
            if edit is not None:
                head = self.history_users(session)
                if not head or head[-1].get('transcriptAnchorId') != edit[1]:
                    raise CloudCliError('CloudCLI history changed after the last Desktop turn', 409)
            descriptors = self.upload_attachments(attachments)
            with closing(self.web_socket(connect, timeout=8 if review else 90)) as ws:
                if progress.stream is not None and progress.stream.disconnected():
                    raise ConnectionAbortedError('Desktop disconnected before CloudCLI dispatch')
                if session is None:
                    try:
                        self.web_request('POST', '/api/projects/create-project',
                                         json.dumps({'path': payload['projectPath']}).encode())
                    except CloudCliError as error:
                        if error.status != 409:
                            raise
                    fields = {'provider': 'claude', 'projectPath': payload['projectPath'],
                              'initialMessage': title_text}
                    created = self.web_request('POST', '/api/providers/sessions', json.dumps(fields).encode())
                    session = created.get('data', {}).get('sessionId')
                    if not isinstance(session, str) or not CLOUDCLI_SESSION_ID.fullmatch(session):
                        raise CloudCliError('CloudCLI did not return an attachment session identifier')
                permission = ('default' if not unrestricted and (progress.native_tools or progress.tool_approvals)
                              else 'bypassPermissions')
                options = {'model': payload['model'], 'attachments': descriptors, 'unattended': True,
                           'permissionMode': 'plan' if plan_mode else permission}
                if review:
                    options.update(permissionMode='dontAsk',
                                   toolsSettings={'allowedTools': [], 'disallowedTools': ['*'], 'skipPermissions': False})
                if 'effort' in payload:
                    options['effort'] = payload['effort']
                if progress.stream is not None and progress.stream.disconnected():
                    raise ConnectionAbortedError('Desktop disconnected before CloudCLI dispatch')
                progress.dispatched = True
                dispatch_ms = int(time.time()*1000)
                progress.dispatch_ms = dispatch_ms
                command = {'type': 'chat.send', 'sessionId': session, 'content': payload['message'], 'options': options}
                if edit is not None:
                    command.update(type='chat.edit-send', anchorId=edit[0])
                ws.send(json.dumps(command, ensure_ascii=False))
                diagnostic(stage='cloudcli_response')
                source = CloudCliSocketEvents(ws, session, time.monotonic()+20 if review else None)
                try:
                    with self.observe_turn(progress, dispatch_ms):
                        return (yield from cloudcli_events(source, progress, session))
                finally:
                    if review and not source.complete:
                        with suppress(OSError, websocket_error):
                            ws.settimeout(2)
                            ws.send(json.dumps({'type': 'chat.abort', 'sessionId': session}))
        except ConnectionAbortedError:
            raise
        except (OSError, ValueError, AttributeError, TypeError, websocket_error):
            raise CloudCliError('CloudCLI chat connection ended before a result was confirmed') from None
        finally:
            if review and isinstance(session, str) and CLOUDCLI_SESSION_ID.fullmatch(session):
                with suppress(CloudCliError):
                    self.web_request('DELETE', f'/api/providers/sessions/{session}?force=true&deletedFromDisk=true', timeout=3)

    def question_token(self):
        authorization = self.config.get('desktopQuestions') or {}
        if 'tokenFile' in authorization:
            return cloudcli_local_token(authorization['tokenFile'])
        with self.approval_lock:
            if self.approval_token is None or time.monotonic() >= self.approval_expires:
                self.approval_token = cloudcli_web_login(self.config['port'], authorization)
                self.approval_expires = time.monotonic() + 86400
            return self.approval_token

    def question_action(self, question, action=None):
        connect, websocket_error = cloudcli_websocket()
        try:
            with closing(self.web_socket(connect)) as socket:
                socket.send(json.dumps({'type': 'chat.subscribe', 'sessions': [{'sessionId': question.session}]}))
                deadline = time.monotonic()+6
                while time.monotonic() < deadline:
                    raw = socket.recv()
                    if not raw or len(raw) > MAX_REQUEST:
                        break
                    event = json.loads(raw)
                    if event.get('kind') != 'chat_subscribed' or event.get('sessionId') != question.session:
                        continue
                    pending = event.get('pendingPermissions') or []
                    if event.get('isProcessing') is True and any(
                        request.get('requestId') == question.request_id and request.get('toolName') == question.tool_name
                        and request.get('input') == question.original for request in pending if isinstance(request, dict)
                    ):
                        if action is not None:
                            socket.send(json.dumps(action, ensure_ascii=False))
                        return
                    raise CloudCliError('This CloudCLI question has already closed', 409)
        except (OSError, ValueError, AttributeError, TypeError, websocket_error):
            pass
        raise CloudCliError('CloudCLI did not accept the Desktop question response', 503)

    def abandon_question(self, turn):
        turn.events.close()
        try:
            self.question_action(turn.question, {'type': 'chat.abort', 'sessionId': turn.question.session})
        except CloudCliError:
            pass

    def expire_question(self, namespace, turn):
        with self.active_lock:
            if self.paused_turns.get(namespace) is not turn:
                return
            del self.paused_turns[namespace]
        self.abandon_question(turn)
        self.invalidate_edit(namespace, turn.question.session, turn.progress)

    def schedule_question_check(self, namespace, turn):
        turn.timer = threading.Timer(60, self.check_paused_question, args=(namespace, turn))
        turn.timer.daemon = True
        turn.timer.start()

    def check_paused_question(self, namespace, turn):
        with self.active_lock:
            if self.paused_turns.get(namespace) is not turn:
                return
        try:
            self.question_action(turn.question)
        except CloudCliError as error:
            if error.status == 409:
                with self.active_lock:
                    if self.paused_turns.get(namespace) is not turn:
                        return
                    del self.paused_turns[namespace]
                turn.events.close()
                self.invalidate_edit(namespace, turn.question.session, turn.progress)
                return
        with self.active_lock:
            if self.paused_turns.get(namespace) is turn:
                self.schedule_question_check(namespace, turn)

    def close(self):
        with self.active_lock:
            turns = list(self.paused_turns.items())
            self.paused_turns.clear()
        for namespace, turn in turns:
            turn.timer.cancel()
            self.abandon_question(turn)
            self.invalidate_edit(namespace, turn.question.session, turn.progress)

    def closed_question_binding(self, db, namespace, histories, card=None):
        receipt = db.execute('SELECT history,card,session,model,project,workspace FROM question_receipts '
                             'WHERE scope=? AND created>=?', (namespace, time.time()-30*86400)).fetchone()
        if not receipt:
            return None
        history, issued_card, session, model, project, workspace = receipt
        if history not in histories or (card is not None and card != issued_card):
            return None
        if any(not isinstance(entry, dict) or entry.get('sessionId') == session for entry in self.running_sessions()):
            raise CloudCliError('The original CloudCLI question is still closing; retry shortly', 409)
        return session, project, workspace, model, None

    def answer(self, manager, data, messages, latest, namespace, stream, check_model=None):
        answers = [part for part in latest if isinstance(part, dict) and part.get('type') == 'tool_result']
        if len(answers) != 1 or any(part is not answers[0] and (not isinstance(part, dict)
                or part.get('type') != 'text' or not isinstance(part.get('text'), str)
                or re.sub(r'<system-reminder>.*?</system-reminder>', '', part['text'], flags=re.S).strip())
                for part in latest):
            raise CloudCliError('Expected a single Claude Desktop answer', 400)
        expected = history_signature(messages[:-1], 'cloudcli')
        answer = {k: v for k, v in answers[0].items() if k != 'cache_control'}
        identity = hashlib.sha256(json.dumps([namespace, expected, answer], sort_keys=True).encode()).hexdigest()
        with self.conversation_slot(namespace), self.configuration_lock():
            with closing(sqlite3.connect(self.path)) as db:
                prior = db.execute('SELECT reply FROM requests WHERE identity=?', (identity,)).fetchone()
            if prior is not None and prior[0] is not None:
                reply = json.loads(prior[0])
                if check_model is not None:
                    check_model(self.resolve_model(reply['model'])[0])
                return reply
            with self.active_lock:
                turn = self.paused_turns.get(namespace)
            if turn is not None and turn.request == identity:
                return turn.reply
            card = answers[0].get('tool_use_id')
            if turn is None and isinstance(card, str):
                with closing(sqlite3.connect(self.path)) as db, db:
                    closed = self.closed_question_binding(db, namespace, [expected], card)
                    if closed:
                        session, project, workspace, model, _ = closed
                        if check_model is not None:
                            check_model(model)
                        reply = self.local_reply(data['model'], '之前的提问已结束，本次选择未提交。可以在这个对话继续提问。')
                        confirmed = history_signature([*messages, reply], 'cloudcli')
                        self.save_session(db, namespace, expected, session, project, workspace, [confirmed])
                        db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?)',
                                   (identity, namespace, json.dumps(reply, ensure_ascii=False), time.time()))
                        db.execute('DELETE FROM question_receipts WHERE scope=?', (namespace,))
                        return reply
            if turn is None or expected != turn.expected or card != turn.question.block['id']:
                raise CloudCliError('No matching pending question for this Desktop conversation', 409)
            if check_model is not None:
                check_model(turn.progress.model)
            action = turn.question.decision(answers[0])
            with self.active_lock:
                if self.paused_turns.get(namespace) is not turn:
                    raise CloudCliError('This Desktop question has expired', 409)
                del self.paused_turns[namespace]
            turn.timer.cancel()
            try:
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('INSERT INTO requests VALUES (?, ?, NULL, ?)', (identity, namespace, time.time()))
                    db.execute('DELETE FROM question_receipts WHERE scope=?', (namespace,))
                self.question_action(turn.question, action)
                turn.progress.stream = stream
                turn.progress.awaiting_decision = turn.question.request_id
                # The card completes the existing run, even if the picker changed meanwhile.
                original_model = turn.reply['model']
                if stream is not None:
                    stream.model = original_model
                return self.advance(manager, {**data, 'model': original_model}, messages, stream, namespace,
                                    identity, turn.events, turn.progress, turn.previous_history, turn.project, turn.workspace)
            except CloudCliStopped:
                raise
            except Exception:
                self.abandon_question(turn)
                self.invalidate_edit(namespace, turn.question.session, turn.progress)
                raise

    def advance(self, manager, data, messages, stream, namespace, identity, events, progress,
                previous_history, project, workspace):
        try:
            question = next(events)
        except (CloudCliStopped, CloudCliNativeStop):
            self.save_interrupted_turn(messages, namespace, progress, previous_history, project, workspace,
                                       stopped=True)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE request_operations SET pending=0 WHERE identity=?', (identity,))
            raise
        except StopIteration as complete:
            session, content, usage = complete.value
            result = self.result(data['model'], stream, content, usage, 'end_turn')
        else:
            content = progress.take_content()+[question.block]
            result = self.result(data['model'], stream, content, dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0), 'tool_use')
            progress.stream = None
            expected = history_signature(messages+[{'role': 'assistant', 'content': content}], 'cloudcli')
            turn = CloudCliPausedTurn(events, progress, question, previous_history, project, workspace, identity,
                                      result, expected)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE request_operations SET pending=0 WHERE identity=?', (identity,))
                db.execute('INSERT OR REPLACE INTO question_receipts VALUES (?,?,?,?,?,?,?,?)',
                           (namespace, expected, question.block['id'], question.session, progress.model,
                            project, workspace, time.time()))
            with self.active_lock:
                self.paused_turns[namespace] = turn
                self.schedule_question_check(namespace, turn)
            return result
        confirmed = history_signature(messages+[{'role': 'assistant', 'content': content}], 'cloudcli')
        anchor = self.turn_anchor(progress, session) if namespace.rpartition(':')[2] else None
        with closing(sqlite3.connect(self.path)) as db, db:
            self.save_session(db, namespace, previous_history, session, project, workspace, [confirmed])
            if namespace.rpartition(':')[2]:
                self.save_turn_state(db, namespace, session, previous_history, progress, anchor)
                db.execute('UPDATE requests SET reply=NULL WHERE scope=? AND identity<>?', (namespace, identity))
            if progress.workspace_prompt_fingerprint is not None:
                db.execute('INSERT OR REPLACE INTO workspace_prompts VALUES (?, ?, ?, ?)',
                           (namespace, session, progress.turn_position, progress.workspace_prompt_fingerprint))
            db.execute('UPDATE requests SET reply=? WHERE identity=?', (json.dumps(result, ensure_ascii=False), identity))
            db.execute('UPDATE request_operations SET pending=0 WHERE identity=?', (identity,))
        model = progress.reported_model or progress.model
        manager.count_usage({'id': result['id'], 'model': model, 'usage': usage}, result['id'])
        return result

    def save_interrupted_turn(self, messages, namespace, progress, prefix, project, workspace, *, stopped=False):
        session = progress.session
        if not namespace.rpartition(':')[2] or not session or not progress.dispatched:
            return False
        anchor = self.turn_anchor(progress, session)
        if not stopped:
            if anchor is None:
                return False
            try:
                running = self.running_sessions()
            except CloudCliError:
                return False
            if any(not isinstance(entry, dict) or entry.get('sessionId') == session for entry in running):
                return False
        # Keep the original request marker. A continuation or an explicit edit can
        # use this checkpoint, but an identical retry must never rerun the task.
        content = progress.take_content()
        histories = [history_signature(messages, 'cloudcli')]
        if content:
            histories.append(history_signature(messages+[{'role': 'assistant', 'content': content}], 'cloudcli'))
        with closing(sqlite3.connect(self.path)) as db, db:
            previous = db.execute('SELECT tip_anchor FROM session_state WHERE scope=? AND session=?',
                                  (namespace, session)).fetchone()
            prior_anchor = previous[0] if previous else None
            self.save_session(db, namespace, prefix, session, project, workspace, histories)
            self.save_turn_state(db, namespace, session, prefix, progress, anchor)
            if stopped and anchor is None and progress.edit_anchor is None and (
                    prior_anchor or prefix == history_signature([], 'cloudcli')):
                db.execute('INSERT OR REPLACE INTO stopped_before_anchor VALUES (?,?,?,?)',
                           (namespace,session,prefix,prior_anchor))
        return True

    @staticmethod
    def save_turn_state(db, namespace, session, prefix, progress, anchor):
        now = time.time()
        db.execute('DELETE FROM stopped_before_anchor WHERE scope=? AND session=?',(namespace,session))
        db.execute('DELETE FROM anchor_receipts WHERE scope=? AND session=?',(namespace,session))
        if anchor is None and progress.forwarded_text and progress.dispatch_ms is not None:
            # CloudCLI can publish its transcript after the final response. Keep
            # just enough information to verify that same native prompt later.
            fingerprint = hashlib.sha256(progress.forwarded_text.encode()).hexdigest()
            db.execute('INSERT INTO anchor_receipts VALUES (?,?,?,?,?,?,?)',
                       (namespace,session,prefix,progress.turn_position,fingerprint,progress.dispatch_ms,int(now*1000)))
        db.execute('INSERT OR REPLACE INTO session_state VALUES (?, ?, ?, ?, ?)',
                   (namespace, session, progress.model, anchor, now))
        if progress.edit_anchor is not None:
            db.execute('DELETE FROM edit_turns WHERE scope=? AND session=? AND position>=?',
                       (namespace, session, progress.turn_position))
            db.execute('DELETE FROM workspace_prompts WHERE scope=? AND session=? AND position>=?',
                       (namespace, session, progress.turn_position))
        if anchor is not None:
            db.execute('INSERT OR REPLACE INTO edit_turns VALUES (?, ?, ?, ?, ?, ?)',
                       (namespace, session, prefix, progress.turn_position, anchor, now))

    def save_session(self, db, namespace, previous_history, session, project, workspace, histories):
        now = time.time()
        db.execute('DELETE FROM sessions WHERE scope=? AND (history=? OR session=?)', (namespace, previous_history, session))
        db.executemany('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)',
                       [(namespace, history, session, now) for history in histories])
        if (project != self.config['projectPath'] or
                workspace is not None and (self.device or 'projectWorkspaces' in (self.config['hostControl'] or {}))):
            db.execute('INSERT OR REPLACE INTO session_projects VALUES (?, ?, ?, ?)', (session, project, workspace, now))

    @staticmethod
    def confirmed_partial_history(db, namespace, partial_history):
        # A final user message here is an explicitly interrupted turn whose reply was lost.
        missing = partial_history[-1]['role'] == 'user'
        text = None if missing else desktop_assistant_text(partial_history[-1])
        if not missing and (not text or not text.strip()):
            return None
        replies = db.execute('SELECT reply FROM requests WHERE scope=? AND reply IS NOT NULL LIMIT 2',
                             (namespace,)).fetchall()
        if len(replies) != 1:
            return None
        reply = json.loads(replies[0][0])
        if not isinstance(reply, dict) or reply.get('stop_reason') != 'end_turn':
            return None
        complete = desktop_assistant_text(reply)
        if not complete:
            return None
        if not missing and text.strip() not in ('Request failed', '请求失败'):
            if len(text) >= len(complete) or not complete.startswith(text):
                return None
        # The caller verifies the reconstructed history against the same conversation's checkpoint.
        original = partial_history if missing else partial_history[:-1]
        return history_signature([*original, reply], 'cloudcli')

    def session_binding(self, db, namespace, prefix, position, compact_context, interrupted_prefixes=(),
                        partial_history=None, legacy_prefix=None):
        fields = ('SELECT DISTINCT sessions.session, project, workspace, model, tip_anchor FROM sessions '
                  'LEFT JOIN session_projects USING (session) '
                  'LEFT JOIN session_state USING (scope, session) WHERE sessions.scope=?')
        if position:
            for candidate in (prefix, *interrupted_prefixes, legacy_prefix):
                if candidate is None:
                    continue
                current = db.execute(fields+' AND history=?', (namespace, candidate)).fetchone()
                if current is not None:
                    return current, None
            if partial_history and namespace.rpartition(':')[2]:
                checkpoint = history_signature(partial_history, 'cloudcli')
                current = db.execute(fields+' AND history=?', (namespace, checkpoint)).fetchone()
                if current is not None:
                    return current, None
                confirmed = self.confirmed_partial_history(db, namespace, partial_history)
                if confirmed is not None:
                    current = db.execute(fields+' AND history=?', (namespace, confirmed)).fetchone()
                    if current is not None:
                        return current, None
        has_scope = bool(namespace.rpartition(':')[2])
        bindings = (db.execute(fields+' LIMIT 2', (namespace,)).fetchall()
                    if has_scope and (compact_context or self.config.get('desktopQuestions')) else [])
        if compact_context and len(bindings) == 1 and self.verified_compact_context(db, namespace, compact_context):
            return bindings[0], None
        if has_scope and self.config.get('desktopQuestions'):
            if bindings:
                if len(bindings) == 1:
                    binding = bindings[0]
                    target = db.execute('SELECT anchor FROM edit_turns WHERE scope=? AND session=? AND prefix=? '
                                        'AND position=?', (namespace, binding[0], prefix, position)).fetchone()
                    if target:
                        tip = binding[4] or self.recover_tip(db,namespace,binding[0])
                        if tip:
                            return binding, (target[0],tip)
                    if binding[4] is None:
                        receipt_matches = db.execute('SELECT 1 FROM anchor_receipts WHERE scope=? AND session=? '
                                                     'AND prefix=? AND position=?',
                                                     (namespace,binding[0],prefix,position)).fetchone()
                        if receipt_matches:
                            tip = self.recover_tip(db,namespace,binding[0])
                            if tip:
                                return binding, (tip,tip)
                        anchor = self.stopped_edit_anchor(db, namespace, prefix, binding[0])
                        if anchor is not None:
                            return binding, (anchor, anchor)
                        if self.stopped_before_user_message(db,namespace,prefix,binding[0]):
                            return binding, None
                raise CloudCliError('No verified CloudCLI message anchor for this Desktop edit', 409)
            if position:
                branch = self.desktop_branch_source(db, namespace, prefix, interrupted_prefixes)
                if branch is not None:
                    return self.fork_desktop_branch(branch)
        if position and history_signature([], 'cloudcli') not in interrupted_prefixes:
            raise CloudCliError('No matching CloudCLI session; start a new Desktop conversation', 409)
        return None, None

    def stopped_before_user_message(self, db, namespace, prefix, session):
        checkpoint = db.execute('SELECT prior_anchor FROM stopped_before_anchor WHERE scope=? AND session=? AND prefix=?',
                                (namespace,session,prefix)).fetchone()
        if checkpoint is None:
            return False
        if any(not isinstance(row,dict) or row.get('sessionId') == session for row in self.running_sessions()):
            raise CloudCliError('CloudCLI is still stopping the previous Desktop turn',409)
        users = self.history_users(session)
        # The provider can stop before appending the old prompt. In that case
        # a verified unchanged transcript accepts the edit as a normal send.
        return (users[-1].get('transcriptAnchorId') if users else None) == checkpoint[0]

    def recover_tip(self, db, namespace, session):
        receipt = db.execute('SELECT fingerprint,dispatched,ended FROM anchor_receipts WHERE scope=? AND session=?',
                             (namespace,session)).fetchone()
        if receipt is None:
            return None
        # The terminal event can arrive just before CloudCLI publishes the user
        # record. Re-read history briefly without submitting the prompt again.
        deadline = time.monotonic() + 2
        while (remaining := deadline-time.monotonic()) > 0:
            anchor = self.resolve_turn_anchor(session, *receipt, timeout=remaining)
            if anchor:
                return anchor
            time.sleep(min(.2,max(0,deadline-time.monotonic())))
        return None

    def desktop_branch_source(self, db, namespace, prefix, interrupted_prefixes):
        # A local rewind may have a new Desktop UUID. Only branch a verified
        # prefix from this same gateway binding (the database is per device).
        same_binding = namespace.rsplit(':',1)[0]+':%'
        for candidate in (prefix,*interrupted_prefixes):
            if candidate == history_signature([], 'cloudcli'):
                continue
            candidates = db.execute("""
                SELECT s.scope,s.session,p.project,p.workspace,t.model,t.tip_anchor,NULL
                FROM sessions s JOIN session_state t USING (scope,session)
                LEFT JOIN session_projects p USING (session)
                WHERE s.scope LIKE ? AND s.scope<>? AND s.history=?
                  AND (t.tip_anchor IS NOT NULL OR EXISTS (SELECT 1 FROM anchor_receipts r
                       WHERE r.scope=s.scope AND r.session=s.session)) LIMIT 2
                """,(same_binding,namespace,candidate)).fetchall()
            if not candidates:
                candidates = db.execute("""
                    SELECT e.scope,e.session,p.project,p.workspace,t.model,t.tip_anchor,e.anchor
                    FROM edit_turns e JOIN session_state t USING (scope,session)
                    LEFT JOIN session_projects p USING (session)
                    WHERE e.scope LIKE ? AND e.scope<>? AND e.prefix=?
                      AND (t.tip_anchor IS NOT NULL OR EXISTS (SELECT 1 FROM anchor_receipts r
                           WHERE r.scope=e.scope AND r.session=e.session))
                    ORDER BY e.updated, e.session
                    """,(same_binding,namespace,candidate)).fetchall()
                if len(candidates) > 1:
                    return self.common_rewind_source(candidates)
            if not candidates:
                candidates = db.execute("""
                    SELECT r.scope,r.session,p.project,p.workspace,t.model,t.tip_anchor,''
                    FROM anchor_receipts r JOIN session_state t USING (scope,session)
                    LEFT JOIN session_projects p USING (session)
                    WHERE r.scope LIKE ? AND r.scope<>? AND r.prefix=? LIMIT 2
                    """,(same_binding,namespace,candidate)).fetchall()
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                raise CloudCliError('More than one CloudCLI conversation matches this Desktop branch', 409)
        return None

    def common_rewind_source(self, candidates):
        # Repeated saves create new Desktop UUIDs for the same rewind point.
        # Identify the unmodified prefix before choosing which native chat to fork.
        error = 'More than one CloudCLI conversation matches this Desktop branch'
        if any(candidate[2:4] != candidates[0][2:4] for candidate in candidates):
            raise CloudCliError(error, 409)
        deadline = time.monotonic() + 12
        histories = [self.history_before_edit(candidate[1], candidate[6], deadline-time.monotonic())
                     for candidate in candidates]
        index = min(range(len(histories)), key=lambda i: len(histories[i][0]))
        prefix, original_prompt = histories[index]
        if not prefix or not isinstance(original_prompt, str) or self.history_timestamp(prefix[0]) is None:
            raise CloudCliError(error, 409)
        for history, _ in histories:
            if history[:len(prefix)] != prefix:
                raise CloudCliError(error, 409)
            # CloudCLI can retain a regenerated copy of the old user prompt
            # between the common prefix and the replacement. Fork the clean
            # source so that this extra prompt is never carried forward again.
            if any(row.get('role') != 'user' or row.get('kind') != 'text'
                   or row.get('content') != original_prompt for row in history[len(prefix):]):
                raise CloudCliError(error, 409)
        return candidates[index]

    def fork_desktop_branch(self, branch):
        source_scope, source, project, workspace, model, tip, anchor = branch
        with self.conversation_slot(source_scope):
            if any(not isinstance(row,dict) or row.get('sessionId') == source for row in self.running_sessions()):
                raise CloudCliError('The original CloudCLI conversation is still running', 409)
            if tip is None:
                with closing(sqlite3.connect(self.path)) as db:
                    tip = self.recover_tip(db,source_scope,source)
                if tip is None:
                    raise CloudCliError('CloudCLI has not published the verified Desktop anchor',409)
            if anchor == '':
                anchor = tip
            users = self.history_users(source)
            if not users or users[-1].get('transcriptAnchorId') != tip:
                raise CloudCliError('CloudCLI history changed after the last Desktop turn', 409)
            previous = None
            if anchor:
                # Read through the target and its preceding user prompt.
                users = self.history_users(source,anchor=anchor)
                matches = [index for index,row in enumerate(users) if row.get('transcriptAnchorId') == anchor]
                if len(matches) != 1 or users[-1].get('transcriptAnchorId') != tip:
                    raise CloudCliError('No verified CloudCLI message anchor for this Desktop branch', 409)
                index = matches[0]
                original = users[index]
                previous = users[index-1] if index else None
            else:
                original = users[-1]
            options = {'upToAnchorId':anchor} if anchor else {}
            result = self.web_request('POST', f'/api/providers/sessions/{source}/fork',
                                      json.dumps(options).encode())
            fork = result.get('data', {}).get('sessionId')
            if not isinstance(fork,str) or fork == source or not CLOUDCLI_SESSION_ID.fullmatch(fork):
                raise CloudCliError('CloudCLI did not return a valid Desktop branch')
            fork_users = self.history_users(fork,minimum=2 if previous else 1)
            if not fork_users:
                raise CloudCliError('CloudCLI did not preserve the verified Desktop prefix',409)
            head = fork_users[-1]
            fork_anchor = head.get('transcriptAnchorId')
            if (not isinstance(fork_anchor,str) or not CLOUDCLI_SESSION_ID.fullmatch(fork_anchor)
                    or head.get('content') != original.get('content')):
                raise CloudCliError('CloudCLI did not preserve the verified Desktop prefix',409)
            # Native forks at a user anchor recreate that prompt with a new
            # timestamp. Check the untouched preceding prompt in that case.
            if previous is not None:
                if (len(fork_users) < 2 or fork_users[-2].get('content') != previous.get('content')
                        or self.history_timestamp(fork_users[-2]) != self.history_timestamp(previous)):
                    raise CloudCliError('CloudCLI did not preserve the verified Desktop prefix',409)
            elif not anchor and self.history_timestamp(head) != self.history_timestamp(original):
                raise CloudCliError('CloudCLI did not preserve the verified Desktop prefix',409)
        return (fork,project,workspace,model,fork_anchor), (fork_anchor,fork_anchor) if anchor else None

    def compact_checkpoint(self, db, namespace, history, model):
        # Desktop trims its local history. CloudCLI already owns the native
        # conversation and will compact its own context when necessary.
        bindings = db.execute('SELECT DISTINCT session FROM sessions WHERE scope=? LIMIT 2', (namespace,)).fetchall()
        if len(bindings) != 1 or not db.execute('SELECT 1 FROM sessions WHERE scope=? AND history=?',
                                                (namespace, history)).fetchone():
            raise CloudCliError('Cannot verify the conversation history for Desktop compaction', 409)
        session = bindings[0][0]
        row = db.execute('SELECT marker FROM compact_receipts WHERE scope=? AND history=? AND session=?',
                         (namespace, history, session)).fetchone()
        if row:
            marker = row[0]
        else:
            marker = 'Continue the existing CloudCLI conversation. SafeCode checkpoint '+uuid.uuid4().hex+'.'
            db.execute('INSERT OR REPLACE INTO compact_receipts VALUES (?, ?, ?, ?)', (namespace, history, session, marker))
        return self.local_reply(model, '<summary>'+marker+'</summary>')

    @staticmethod
    def verified_compact_context(db, namespace, context):
        # The local CLI replaces its history after a compaction request. Only
        # accept a replacement anchored in this scope's latest confirmed reply.
        marker = db.execute('SELECT marker FROM compact_receipts JOIN sessions USING (scope, session, history) '
                            'WHERE scope=?', (namespace,)).fetchone()
        if marker and (context.strip() == marker[0] or context.startswith(marker[0]+'\n')):
            return True
        row = db.execute('SELECT reply FROM requests WHERE scope=? ORDER BY created DESC LIMIT 1',
                         (namespace,)).fetchone()
        if not row or row[0] is None:
            return False
        reply = json.loads(row[0])
        if reply.get('stop_reason') != 'end_turn':
            return False
        text = ''.join(item['text'] for item in reply['content'] if item.get('type') == 'text')
        summaries = re.findall(r'<summary>(.*?)</summary>', text, re.S)
        if len(summaries) != 1:
            return False
        summary = summaries[0].strip()
        # The native compact renderer collapses runs of blank lines before
        # inserting the summary into the next request. Match either rendering.
        return any(value and (context.strip() == value or context.startswith(value+'\n'))
                   for value in (summary, re.sub(r'\n{3,}', '\n\n', summary)))

    def stopped_edit_anchor(self, db, namespace, prefix, session):
        # Older gateways saved interrupted history hashes without the edit
        # anchor. An incoming edit may prove its original prefix against the
        # pending dispatch marker and the latest native user message.
        checkpoint = db.execute('SELECT updated FROM session_state WHERE scope=? AND session=?',
                                (namespace, session)).fetchone()
        pending = db.execute('SELECT COALESCE(operation, identity), created, reply FROM requests '
                             'LEFT JOIN request_operations USING (identity) WHERE scope=? '
                             'ORDER BY created DESC LIMIT 1', (namespace,)).fetchone()
        if checkpoint is None or pending is None or pending[2] is not None or pending[1] > checkpoint[0]:
            return None
        dispatched = int(pending[1]*1000)
        try:
            users = self.history_users(session, since=dispatched)
        except CloudCliError:
            return None
        if not users:
            return None
        last = users[-1]
        anchor, task = last.get('transcriptAnchorId'), last.get('content')
        seen = self.history_timestamp(last)
        if (not isinstance(anchor, str) or not CLOUDCLI_SESSION_ID.fullmatch(anchor)
                or not isinstance(task, str) or seen is None
                or not dispatched <= seen <= int(checkpoint[0]*1000)):
            return None
        identity = hashlib.sha256(json.dumps([namespace, prefix, task], ensure_ascii=False).encode()).hexdigest()
        if identity != pending[0]:
            return None
        if any(row.get('content') == task and row.get('transcriptAnchorId') != anchor
               and (timestamp := self.history_timestamp(row)) is not None and timestamp >= dispatched
               for row in users):
            return None
        return anchor

    def invalidate_edit(self, namespace, session, progress):
        if progress.edit_anchor is None or not progress.dispatched:
            return
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM sessions WHERE scope=? AND session=?', (namespace, session))
            db.execute('DELETE FROM edit_turns WHERE scope=? AND session=?', (namespace, session))
            db.execute('DELETE FROM session_state WHERE scope=? AND session=?', (namespace, session))
            db.execute('DELETE FROM workspace_prompts WHERE scope=? AND session=?', (namespace, session))
            db.execute('DELETE FROM stopped_before_anchor WHERE scope=? AND session=?', (namespace, session))
            db.execute('DELETE FROM anchor_receipts WHERE scope=? AND session=?', (namespace, session))

    @contextmanager
    def conversation_slot(self, conversation, history=None, stopped_requests=(), stream=None):
        turn = ActiveCloudCliTurn(history)
        with self.active_lock:
            active = self.active_conversations.get(conversation)
            if active is None:
                self.active_conversations[conversation] = turn
            elif not self.config.get('desktopQuestions') and (active.client_detached.is_set()
                                                              or active.history in stopped_requests):
                raise CloudCliError(API_ONLY_TURN_RUNNING, 400)
            elif active.history not in stopped_requests:
                raise CloudCliError('Another turn is already running in this CloudCLI conversation', 409)
        if active is not None:
            # Desktop may send the next message before the relay closes the old
            # stream. Only a stop marker for that exact request can cancel it.
            active.stop_requested.set()
            if not active.finished.wait(15):
                raise CloudCliError('CloudCLI is still stopping the previous Desktop turn', 409)
            if stream is not None and stream.disconnected():
                raise ConnectionAbortedError('Desktop disconnected while stopping the previous turn')
            with self.active_lock:
                if conversation in self.active_conversations:
                    raise CloudCliError('Another turn is already running in this CloudCLI conversation', 409)
                self.active_conversations[conversation] = turn
        try:
            yield turn
        finally:
            with self.active_lock:
                del self.active_conversations[conversation]
                turn.finished.set()

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

    def request_previous_stop(self, data, session_header):
        scope = conversation_scope(data, session_header)
        messages = data.get('messages')
        if (not scope or not isinstance(messages, list) or not messages
                or any(not isinstance(item, dict) or item.get('role') not in ('user', 'assistant', 'system')
                       for item in messages)
                or messages[-1]['role'] != 'user'):
            return
        visible = without_desktop_model_commands([item for item in messages if item['role'] != 'system'])
        _, _, _, stopped = split_desktop_interruptions(visible)
        with self.active_lock:
            active = self.active_conversations.get(self.identity+':'+scope)
            if active is None:
                return
            marked = active.history in stopped
            if not self.config.get('desktopQuestions'):
                if marked or active.client_detached.is_set():
                    raise CloudCliError(API_ONLY_TURN_RUNNING, 400)
            elif marked:
                active.stop_requested.set()

    def run(self, manager, data, context_1m, session_header, stream=None, *, check_model=None):
        if not isinstance(data, dict) or not isinstance(data.get('model'), str):
            raise CloudCliError('A configured CloudCLI model is required', 400)
        effort = data.get('output_config', {})
        effort = effort.get('effort') if isinstance(effort, dict) else None
        if effort not in (None, 'default') and (not isinstance(effort, str) or effort not in CLOUDCLI_EFFORTS):
            raise CloudCliError('Unsupported Claude effort', 400)
        model, effort = self.resolve_model(data['model'], context_1m, effort)
        if check_model is not None:
            check_model(model)
        original = data.get('messages')
        if not isinstance(original, list) or any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant')
                                                  for m in original):
            raise CloudCliError('Invalid CloudCLI messages', 400)
        messages = [m for m in original if m['role'] != 'system']
        if not messages or messages[-1]['role'] != 'user':
            raise CloudCliError('CloudCLI messages must end with a user task', 400)
        scope = conversation_scope(data, session_header)
        interrupted_prefixes = []
        partial_history = None
        stopped_requests = ()
        legacy_prefix = None
        if scope:
            visible = without_desktop_model_commands(split_compact_followup(messages))
            if visible is not messages:
                legacy_prefix = history_signature(messages[:-1], 'cloudcli')
            messages, interrupted_prefixes, partial_history, stopped_requests = split_desktop_interruptions(visible)
            if partial_history is None and len(messages) >= 2:
                previous = messages[:-1]
                if previous[-1]['role'] == 'assistant' or history_signature(previous, 'cloudcli') in stopped_requests:
                    partial_history = previous
        if not messages or messages[-1]['role'] != 'user':
            raise CloudCliError('CloudCLI requires a new user task after stopping', 400)
        latest = blocks(messages[-1].get('content'))
        namespace = self.identity+':'+scope
        if any(isinstance(part, dict) and part.get('type') == 'tool_result' for part in latest):
            if not scope or self.config.get('desktopQuestions') is None:
                raise CloudCliError('No matching pending question for this Desktop conversation', 409)
            return self.answer(manager, data, messages, latest, namespace, stream, check_model)
        texts, attachments = [], []
        for part in latest:
            if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                texts.append(part['text'])
            elif isinstance(part, dict) and part.get('type') in ('image', 'document'):
                if len(attachments) == 10:
                    raise CloudCliError('Desktop can send up to 10 attachments per turn', 413)
                attachments.append(cloudcli_attachment(part, len(attachments)+1))
            else:
                raise CloudCliError('CloudCLI cannot use this Desktop content block', 400)
        plan_mode = any(PLAN_MODE_REMINDER.search(text) for text in texts)
        task = '\n'.join(SYSTEM_REMINDER.sub('', text) for text in texts).strip()
        if scope and self.device and self.device['platform'] == 'macos':
            attachments.extend(desktop_read_attachments(messages, task, len(attachments)+1))
        if len(attachments) > 10:
            raise CloudCliError('Desktop can send up to 10 attachments per turn', 413)
        # The composer can include an already executed /compact command in
        # the next message. Keep its raw text only for request identity.
        dispatch_task = DESKTOP_LOCAL_COMMAND.sub('', task).strip() if '<local-command-caveat>' in task else task
        chat_mode = bool(attachments) or plan_mode
        unrestricted = manager.unrestricted_tools and not plan_mode and bool(self.config.get('desktopQuestions'))
        approval_transport = self.config.get('desktopToolApprovals', False)
        tool_approvals = approval_transport and not unrestricted
        if chat_mode and not self.config.get('desktopQuestions'):
            raise CloudCliError('此网关需要连接 CloudCLI 网页账户才能使用截图、附件或计划模式。请联系网关管理员，或改发普通文本。', 400)
        if not dispatch_task and not attachments:
            raise CloudCliError('CloudCLI requires text for the user task', 400)
        # Model checks can carry the current Desktop conversation ID. Reply
        # before opening a conversation slot or considering a history edit.
        if not chat_mode and desktop_model_probe(data, original, latest):
            return self.local_reply(data['model'], 'OK')
        if (not chat_mode and len(original) == 1 and not scope
                and not data.get('tools') and not data.get('system') and task == '.'):
            raise CloudCliError('CloudCLI requires text for the user task', 400)
        title = cloudcli_title_response(task) if not chat_mode and len(messages) == 1 else None
        if title is not None:
            return self.local_reply(data['model'], title)
        if not chat_mode and desktop_suggestion(messages):
            return self.local_reply(data['model'], '')
        if not chat_mode and webfetch_summary_request({**data, 'messages': messages}):
            raise CloudCliError('Desktop WebFetch helper has no matching CloudCLI tool', 400)
        if scope and len(messages) > 1 and not chat_mode and desktop_compaction_request(data.get('system', ''), task):
            before = history_signature(messages[:-1], 'cloudcli')
            with (self.conversation_slot(namespace), self.configuration_lock(),
                  closing(sqlite3.connect(self.path)) as db, db):
                if namespace in self.paused_turns:
                    raise CloudCliError('This CloudCLI conversation is waiting for a Desktop answer', 409)
                return self.compact_checkpoint(db, namespace, before, data['model'])
        forwarded_task = (manager.review_prompt(dispatch_task)
                          if dispatch_task and manager.review_prompt is not None else dispatch_task)
        payload = {'provider': 'claude', 'message': forwarded_task or '请查看本轮附加的文件。',
                   'model': model, 'stream': True}
        family = cloudcli_model_family(model)
        diagnostic(stage='session_route', **({'modelFamily': family} if family else {}))
        if effort not in (None, 'default'):
            payload['effort'] = effort
        # Scope is only used as a local lookup key. Never use a Desktop ID as a
        # CloudCLI session ID. Per-key gateway directories isolate these tables.
        before = history_signature(messages[:-1], 'cloudcli')
        now = time.time()
        legacy_identity = None
        if scope or len(messages)>1:
            request_identity = [namespace, before, task]
            if chat_mode:
                request_identity += [plan_mode, [(name, media, hashlib.sha256(raw).hexdigest())
                                                for name, media, raw in attachments]]
            legacy_identity = hashlib.sha256(json.dumps(request_identity, ensure_ascii=False).encode()).hexdigest()
            request_identity.append([model, effort if effort != 'default' else None])
            identity = hashlib.sha256(json.dumps(request_identity, ensure_ascii=False).encode()).hexdigest()
        else:
            identity = uuid.uuid4().hex
        conversation = namespace if scope else before if len(messages) > 1 else identity
        history = history_signature(messages, 'cloudcli') if scope and stream is not None else None
        with (self.conversation_slot(conversation, history, stopped_requests, stream) as active_turn,
              self.configuration_lock()):
            paused = self.paused_turns.get(namespace)
            if paused is not None:
                if paused.request == identity:
                    return paused.reply
                if paused.expected not in interrupted_prefixes:
                    raise CloudCliError('This CloudCLI conversation is waiting for a Desktop answer', 409)
                # Desktop explicitly stopped this exact card before sending a new task.
                paused.timer.cancel()
                self.expire_question(namespace, paused)
            native_tools = set()
            tool_choice = data.get('tool_choice')
            tool_choice = tool_choice if isinstance(tool_choice, dict) else {}
            offered = data.get('tools')
            if scope and self.config.get('desktopQuestions') and isinstance(offered, list):
                for tool in offered:
                    name = tool.get('name') if isinstance(tool, dict) else None
                    if name == 'AskUserQuestion' or (plan_mode and name == 'ExitPlanMode'):
                        if (tool_choice.get('type') in (None, 'auto', 'any')
                                or (tool_choice.get('type') == 'tool' and tool_choice.get('name') == name)):
                            native_tools.add(name)
            position = 0 if before == history_signature([], 'cloudcli') else len(messages)-1
            workspace_prompt_fingerprint = None
            with closing(sqlite3.connect(self.path)) as db, db:
                with db:
                    db.execute('DELETE FROM requests WHERE created<? AND NOT EXISTS '
                               '(SELECT 1 FROM request_operations o WHERE o.identity=requests.identity AND o.pending=1)',
                               (now-30*86400,))
                    db.execute('DELETE FROM request_operations WHERE NOT EXISTS '
                               '(SELECT 1 FROM requests WHERE requests.identity=request_operations.identity)')
                    db.execute('DELETE FROM workspace_prompts WHERE NOT EXISTS '
                               '(SELECT 1 FROM sessions WHERE sessions.scope=workspace_prompts.scope '
                               'AND sessions.session=workspace_prompts.session)')
                    db.execute('DELETE FROM stopped_before_anchor WHERE NOT EXISTS '
                               '(SELECT 1 FROM sessions WHERE sessions.scope=stopped_before_anchor.scope '
                               'AND sessions.session=stopped_before_anchor.session)')
                    db.execute('DELETE FROM anchor_receipts WHERE NOT EXISTS '
                               '(SELECT 1 FROM sessions WHERE sessions.scope=anchor_receipts.scope '
                               'AND sessions.session=anchor_receipts.session)')
                prior = db.execute('SELECT reply FROM requests WHERE identity=?', (identity,)).fetchone()
                if prior is None and legacy_identity is not None:
                    legacy = db.execute('SELECT reply FROM requests WHERE identity=?', (legacy_identity,)).fetchone()
                    # Older receipts did not record effort. Preserve uncertain
                    # submissions and replay completed receipts only for their model.
                    if legacy is not None and (legacy[0] is None or json.loads(legacy[0]).get('model') == data['model']):
                        prior = legacy
                    elif db.execute('SELECT 1 FROM request_operations WHERE operation=? AND pending=1',
                                    (legacy_identity,)).fetchone():
                        prior = (None,)
                if prior is not None:
                    if prior[0] is None:
                        if (scope and not self.config.get('desktopQuestions') and
                                db.execute('SELECT 1 FROM sessions WHERE scope=? AND history=?',
                                           (namespace, history_signature(messages, 'cloudcli'))).fetchone()):
                            raise CloudCliError('本轮已中断。请在原对话发送下一条消息。', 400)
                        raise CloudCliError('上一轮已提交，尚无已确认的结果。请先在 CloudCLI 核对执行状态。', 409)
                    return json.loads(prior[0])
                compact_context = desktop_compact_context(messages[:-1]) if scope else None
                saved = self.closed_question_binding(db, namespace, interrupted_prefixes) if interrupted_prefixes else None
                edit = None
                if saved is None:
                    saved, edit = self.session_binding(db, namespace, before, position, compact_context,
                                                       interrupted_prefixes, partial_history, legacy_prefix)
                if saved:
                    payload['sessionId'], project, workspace, previous_model, _ = saved
                    if scope and self.config.get('desktopQuestions') and previous_model != model:
                        chat_mode = True
                    default, default_workspace = self.default_project()
                    host = self.config['hostControl']
                    if self.device:
                        if not project:
                            raise CloudCliError('The paired session lacks a device project', 409)
                        if workspace is None:
                            workspace = self.allowed_workspace(self.desktop_directory(data, scope))
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
                else:
                    project, workspace = self.project_for_directory(self.desktop_directory(data, scope))
                # The Agent API runs unattended in bypassPermissions and can skip
                # the permission event that carries a pending AskUserQuestion.
                chat_mode = chat_mode or bool(native_tools) or edit is not None or approval_transport
                if chat_mode:
                    cloudcli_websocket()
                    self.question_token()
                payload['projectPath'] = project
                host_instruction = self.host_instruction(project, workspace)
                if host_instruction:
                    fingerprint = hashlib.sha256(host_instruction.encode()).hexdigest()
                    known = db.execute('SELECT 1 FROM workspace_prompts '
                                       'WHERE scope=? AND session=? AND position<? AND fingerprint=?',
                                       (namespace, payload.get('sessionId'), position, fingerprint)).fetchone()
                    if known is None:
                        # Keep the user's opening words available for the CloudCLI title.
                        payload['message'] += '\n\n'+host_instruction
                        workspace_prompt_fingerprint = fingerprint
                if attachments:
                    payload['message'] += ('\n\n本轮上传的文件请通过 CloudCLI 提供的附件读取。'
                                           '附带的本机路径只是原始引用，本机项目文件仍走已授权设备。')
                self.prepare_project(project, workspace)
                # Commit before POST. A lost connection or process exit cannot
                # cause an automatic second dispatch of this scoped task.
                db.execute('DELETE FROM question_receipts WHERE scope=? OR created<?', (namespace, now-30*86400))
                db.execute('INSERT INTO requests VALUES (?, ?, NULL, ?)', (identity, namespace, now))
                if legacy_identity is not None:
                    db.execute('INSERT INTO request_operations VALUES (?, ?, 1)', (identity, legacy_identity))
            with manager.lock:
                manager.started += 1
            progress = CloudCliProgress(stream, self.config.get('webOrigin'), native_tools, model, tool_approvals,
                                        self.device)
            progress.account_footer = manager.account_footer
            progress.stop_requested = active_turn.stop_requested
            progress.client_detached = active_turn.client_detached
            progress.forwarded_text = payload['message']
            progress.workspace_prompt_fingerprint = workspace_prompt_fingerprint
            progress.turn_position = position
            progress.edit_anchor = edit[0] if edit is not None else None
            events = (self.call_chat(payload, progress, attachments, plan_mode, edit, title_text=task,
                                     unrestricted=unrestricted) if chat_mode
                      else self.call(payload, progress))
            try:
                return self.advance(manager, data, messages, stream, namespace, identity, events, progress,
                                    before, project, workspace)
            except (CloudCliStopped, CloudCliNativeStop):
                raise
            except Exception as error:
                events.close()
                rejected = isinstance(error, CloudCliRejected)
                if not progress.dispatched or rejected:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute('DELETE FROM requests WHERE identity=? AND reply IS NULL', (identity,))
                        db.execute('DELETE FROM request_operations WHERE identity=?', (identity,))
                elif not (isinstance(error, (CloudCliError, ConnectionAbortedError))
                          and self.save_interrupted_turn(messages, namespace, progress, before, project, workspace)):
                    self.invalidate_edit(namespace, payload.get('sessionId'), progress)
                raise

    @staticmethod
    def result(model, stream, content, usage, stop_reason):
        return {'id': stream.message_id if stream is not None else 'msg_'+uuid.uuid4().hex,
                'type': 'message', 'role': 'assistant', 'model': model, 'content': content,
                'stop_reason': stop_reason, 'stop_sequence': None, 'usage': usage}

    @staticmethod
    def local_reply(model, text):
        return CloudCliBackend.result(model, None, [{'type': 'text', 'text': text}] if text else [],
                                      dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0), 'end_turn')


# TODO: Remove the legacy direct Claude Code backend (DESKTOP_LAUNCHER, Channel, and Conversation).
# SafeCode API requests must go through CloudCliBackend so CloudCLI owns Claude Code execution.
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
            provider_status = None
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
                            provider_status = anthropic_error(line) or provider_status
                            for block in blocks(line.get('message', {}).get('content', [])):
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    provider_status = anthropic_error(line, block.get('text')) or provider_status
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
                            status = anthropic_error(line) or provider_status
                            if native_error=='max_output_tokens' or (native_error is None and status is None and (stream_message.stop=='max_tokens' or line.get('stop_reason')=='max_tokens')):
                                truncated=True;content=stream_message.partial()
                            else:
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
    account_footer = None
    unrestricted_tools = False

    def __init__(self, directory, config, *, require_cloudcli=False, review_prompt=None):
        self.directory, self.config = Path(directory), config
        self.review_prompt = review_prompt
        settings = config['cloudcli'] if 'cloudcli' in config else load_cloudcli_config()
        if settings is not None:
            settings = cloudcli_config(settings)
        if require_cloudcli and settings is None:
            raise GatewayError(CLOUDCLI_REQUIRED, 503)
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
        return {'ok': True, 'bridge': 'vps-claude-code', 'backend': 'cloudcli' if self.cloudcli else 'claude_code',
                'instance': self.config['instance'], 'pid': os.getpid(),
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

    def run(self, data, context_1m=False, session_header=None, stream=None, *, check_model=None):
        if self.cloudcli is not None:
            return self.cloudcli.run(self, data, context_1m, session_header, stream,
                                     check_model=check_model)
        # TODO: Delete this legacy branch. The API requires CloudCLI and returns an error if it is unavailable.
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
        if self.cloudcli is not None:
            self.cloudcli.close()
        for c in list(self.conversations):c.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, manager):
        self.manager = manager
        super().__init__(('127.0.0.1', manager.config.get('listenPort', 0)), Handler)


class CloudCliMessagesStream:
    def __init__(self, handler, model, *, send_events=True):
        self.send_events = send_events
        self.handler = handler
        self.model = model
        self.message_id = 'msg_'+uuid.uuid4().hex
        self.started = False
        self.queued = False
        self.index = 0
        self.open_kind = None
        self.wire_lock = threading.Lock()
        self.last_output = time.monotonic()

    def ping_if_idle(self, finished):
        with self.wire_lock:
            if finished.is_set() or not (self.started or self.queued) or time.monotonic()-self.last_output < CLOUDCLI_SSE_PING_INTERVAL:
                return True
            try:
                self.handler.send_sse('ping', {'type': 'ping'})
                self.last_output = time.monotonic()
                return True
            except OSError:
                return False

    def queue_ping(self):
        if self.send_events and time.monotonic()-self.last_output >= 5:
            self.handler.start_sse()
            self.handler.send_sse('ping', {'type': 'ping'})
            self.queued = True
            self.last_output = time.monotonic()

    def disconnected(self):
        connection = self.handler.connection
        try:
            if hasattr(select, 'POLLRDHUP'):
                events = select.POLLRDHUP | select.POLLHUP | select.POLLERR | select.POLLNVAL
                poll = select.poll()
                poll.register(connection, events)
                return any(flags & events for _, flags in poll.poll(0))
            if not select.select([connection], [], [], 0)[0]:
                return False
            with socket.socket(fileno=os.dup(connection.fileno())) as tcp:
                return not tcp.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            return True

    def text(self, part):
        self.write('text', part)

    def thinking(self, part):
        self.write('thinking', part)

    def write(self, kind, part):
        if not self.send_events or not part:
            return
        with self.wire_lock:
            self.write_part(kind, part)

    def write_part(self, kind, part):
        try:
            if not self.started:
                self.started = True
                self.handler.start_sse()
                self.handler.send_sse('message_start', {'type': 'message_start', 'message': {
                    'id': self.message_id, 'type': 'message', 'role': 'assistant', 'model': self.model,
                    'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': dict.fromkeys(CLOUDCLI_USAGE_FIELDS, 0),
                }})
            if self.open_kind != kind:
                self.close_block()
                block = {'type': kind, kind: ''}
                if kind == 'thinking':
                    block['signature'] = ''
                self.handler.send_sse('content_block_start', {
                    'type': 'content_block_start', 'index': self.index, 'content_block': block,
                })
                self.open_kind = kind
            self.handler.send_sse('content_block_delta', {
                'type': 'content_block_delta', 'index': self.index, 'delta': {'type': kind+'_delta', kind: part},
            })
            self.last_output = time.monotonic()
        except OSError:
            raise ConnectionAbortedError('Desktop disconnected during a CloudCLI stream') from None

    def close_block(self):
        if self.open_kind is not None:
            self.handler.send_sse('content_block_stop', {'type': 'content_block_stop', 'index': self.index})
            self.index += 1
            self.open_kind = None

    def finish(self, response):
        with self.wire_lock:
            self.close_block()
            for index, block in enumerate(response['content'][self.index:], self.index):
                for event, payload in sse_content_block(block, index):
                    self.handler.send_sse(event, payload)
            for event, payload in sse_tail(response):
                self.handler.send_sse(event, payload)


class Handler(BaseHTTPRequestHandler):
    sse_started = False
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
        if self.sse_started:
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.sse_started = True

    def send_sse(self, event, payload):
        self.wfile.write(('event: '+event+'\ndata: '+json.dumps(payload, ensure_ascii=False)+'\n\n').encode())
        self.wfile.flush()

    def message_error(self, stream, code, error_type, message):
        payload = {'type': 'error', 'error': {'type': error_type, 'message': message}}
        try:
            if stream is not None and self.sse_started:
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

    def execute_message(self, data, **options):
        if not self.manager.capacity.acquire(blocking=False):
            raise GatewayError('Too many active Desktop requests', 429)
        try:
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
            if not 0 < length <= MAX_MESSAGES_REQUEST:
                raise GatewayError('Messages request exceeds 64 MiB or is empty', 413)
            self.connection.settimeout(30)
            diagnostic(requestBytes=length)
            data = json.loads(self.rfile.read(length))
            diagnostic(stage='gateway_validation')
            if isinstance(data, dict):
                for field, name in (('messages', 'messageCount'), ('tools', 'toolCount')):
                    if isinstance(data.get(field), list):
                        diagnostic(**{name: min(1000000, len(data[field]))})
            if path == COUNT_TOKENS_PATH:
                return self.message_error(None, 404, 'not_found_error', 'Token counting is unavailable on this CLI bridge')
            if isinstance(data, dict) and self.manager.cloudcli is not None:
                stream = CloudCliMessagesStream(self, data.get('model'), send_events=bool(data.get('stream')))
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
        yield from sse_content_block(block, index)
    yield from sse_tail(response)


def sse_content_block(block, index):
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


def sse_tail(response):
    yield 'message_delta', {'type': 'message_delta', 'delta': {'stop_reason': response['stop_reason'], 'stop_sequence': response.get('stop_sequence')}, 'usage': response['usage']}
    yield 'message_stop', {'type': 'message_stop'}


def serve(directory):
    directory = Path(directory)
    config = json.loads((directory/'config.json').read_text())
    manager = Manager(directory, {**config, 'cloudcli': require_cloudcli_config()}, require_cloudcli=True)
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
    try:
        verify_cloudcli_agent(require_cloudcli_config())
    except GatewayError as error:
        return {'ready': False, 'port': 0, 'instance': '', 'warning': str(error)}
    config = {'token': request['token'], 'instance': request['instance']}
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
