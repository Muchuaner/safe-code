#!/usr/bin/env python3
"""CloudCLI stdio tools for a folder on a Mac authorized in SafeCode."""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import time


MAX_TEXT = 64 * 1024
MAX_FRAME = 6 * MAX_TEXT
SSH_TIMEOUT = 30

# The remote command is constant. User paths and file contents travel as JSON on
# stdin. Directory descriptors and no-follow opens keep traversal inside the
# selected workspace without relying on the model or the remote shell.
REMOTE_SCRIPT = r'''
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

MAX_TEXT = 64 * 1024
MAX_FRAME = 6 * MAX_TEXT
OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def parts(value, root=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > 4096:
        raise ValueError()
    if value.startswith('/') != root:
        raise ValueError()
    if not root and value == '.':
        return []
    parts = value[1:].split('/') if root else value.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ValueError()
    return parts


def directory(start, components):
    fd = os.dup(start)
    try:
        for component in components:
            child = os.open(component, OPEN_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def list_files(root, args):
    fd = directory(root, parts(args['path']))
    try:
        items = []
        truncated = False
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(items) == 200:
                    truncated = True
                    break
                kind = ('symlink' if entry.is_symlink() else
                        'directory' if entry.is_dir(follow_symlinks=False) else
                        'file' if entry.is_file(follow_symlinks=False) else 'other')
                items.append({'name': entry.name, 'type': kind})
        return {'entries': sorted(items, key=lambda e: e['name']), 'truncated': truncated}
    finally:
        os.close(fd)


def read_text(root, args):
    *parents, name = parts(args['path'])
    parent = directory(root, parents)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEXT:
                return {'error': 'This tool only reads regular text files up to 64 KiB.'}
            data = stream.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT:
                return {'error': 'This tool only reads regular text files up to 64 KiB.'}
            return {'text': data.decode('utf-8')}
    finally:
        os.close(parent)


def create_text(root, args):
    *parents, name = parts(args['path'])
    parent = directory(root, parents)
    temp = '.safecode-' + uuid.uuid4().hex
    try:
        data = args['text'].encode('utf-8')
        if len(data) > MAX_TEXT:
            return {'error': 'This tool only creates text files up to 64 KiB.'}
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.unlink(temp, dir_fd=parent)
        return {'created': True}
    finally:
        os.close(parent)


def move_path(root, args):
    *source_parents, source = parts(args['source'])
    *target_parents, target = parts(args['destination'])
    source_dir = directory(root, source_parents)
    try:
        target_dir = directory(root, target_parents)
        try:
            info = os.stat(source, dir_fd=source_dir, follow_symlinks=False)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return {'error': 'Only regular files and directories can be moved.'}
            # macOS RENAME_EXCL atomically refuses to replace an existing name.
            rename = ctypes.CDLL(None, use_errno=True).renameatx_np
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(source_dir, os.fsencode(source), target_dir, os.fsencode(target), 0x00000004):
                raise OSError(ctypes.get_errno(), 'Exclusive move failed')
            return {'moved': True}
        finally:
            os.close(target_dir)
    finally:
        os.close(source_dir)


def desktop_workspace(session):
    guid = '[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
    if not isinstance(session, str) or not re.fullmatch(guid, session):
        return {}
    root = Path.home()/'Library/Application Support/Claude-3p/claude-code-sessions'
    if root.resolve() != root:
        return {}
    matches = set()
    for path in root.glob('*/*/local_*.json'):
        account, organization, name = path.relative_to(root).parts
        if not (re.fullmatch(guid, account) and re.fullmatch(guid, organization)
                and re.fullmatch('local_'+guid+'\\.json', name)):
            continue
        if path.resolve() != path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1024*1024:
                    continue
                record = json.loads(stream.read(1024*1024+1))
            prior = record.get('priorCliSessionIds')
            if session != record.get('cliSessionId') and not (isinstance(prior, list) and session in prior):
                continue
            place = record.get('folderPlace')
            cwd = (place.get('cwd') if isinstance(place, dict) else None) or record.get('originCwd') or record.get('cwd')
            if isinstance(cwd, str) and len(cwd) <= 4096:
                parts(cwd, root=True)
                matches.add(cwd)
        except (OSError, AttributeError, ValueError):
            continue
    return {'workspace': matches.pop()} if len(matches) == 1 else {}


def request():
    if sys.platform != 'darwin':
        return {'error': 'These tools require an authorized Mac.'}
    payload = sys.stdin.buffer.read(MAX_FRAME + 1)
    if len(payload) > MAX_FRAME:
        return {'error': 'Tool request is too large.'}
    call = json.loads(payload)
    if call['name'] == 'desktop_workspace':
        return desktop_workspace(call['session'])
    operation = {
        'list_files': list_files,
        'read_text_file': read_text,
        'create_text_file': create_text,
        'move_path': move_path,
    }[call['name']]
    start = os.open('/', OPEN_DIRECTORY)
    try:
        root = directory(start, parts(call['workspace'], root=True))
        try:
            return operation(root, call['arguments'])
        finally:
            os.close(root)
    finally:
        os.close(start)


try:
    result = request()
except UnicodeError:
    result = {'error': 'The file is not valid UTF-8 text.'}
except OSError as error:
    result = {'error': {
        errno.ENOENT: 'The workspace or requested path does not exist.',
        errno.EACCES: 'The Mac account cannot access this workspace or path.',
        errno.EPERM: 'The Mac account cannot access this workspace or path.',
        errno.EEXIST: 'The destination already exists.',
        errno.ELOOP: 'Symlinks are unavailable in this workspace.',
        errno.ENOTDIR: 'A path component is a symlink or is not a directory.',
        errno.EXDEV: 'This move crosses filesystems.',
    }.get(error.errno, 'The Mac could not complete the file operation.')}
except (KeyError, TypeError, ValueError):
    result = {'error': 'Use a relative path inside the selected workspace.'}
print(json.dumps(result))
'''

REMOTE_COMMAND = shlex.join(['/usr/bin/python3', '-I', '-B', '-c', REMOTE_SCRIPT])


def string_field(description):
    return {'type': 'string', 'description': description}


PATH_FIELD = string_field('Relative path within the selected Mac workspace.')
TOOL_DEFINITIONS = {
    'list_files': (
        'List files in a folder on the connected Mac. Use "." for the workspace root.',
        {'path': PATH_FIELD},
    ),
    'read_text_file': (
        'Read a UTF-8 text file on the connected Mac, up to 64 KiB.',
        {'path': PATH_FIELD},
    ),
    'create_text_file': (
        'Create a new UTF-8 text file on the connected Mac. The operation refuses existing files.',
        {'path': PATH_FIELD, 'text': string_field('Text to write, up to 64 KiB.')},
    ),
    'move_path': (
        'Move or rename a file or directory inside the selected Mac workspace. The destination must not exist.',
        {'source': PATH_FIELD, 'destination': PATH_FIELD},
    ),
}
PROTOCOL_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}


class ToolError(Exception):
    pass


def private_path(path, file_type):
    info = path.lstat()
    owner = info.st_uid == os.getuid()
    private = not info.st_mode & 0o077
    return owner and private and file_type(info.st_mode)


def bridge_arguments(client_id):
    if not re.fullmatch('[a-f0-9]{20}', client_id):
        raise ToolError('Invalid SafeCode host control identifier.')
    home = Path.home().resolve()
    directory = home / '.ssh/safe-code-host' / client_id
    config = directory / 'config'
    identity = directory / 'identity'
    known = directory / 'known_hosts'
    try:
        if directory.resolve(strict=True) != directory or not private_path(directory, stat.S_ISDIR):
            raise ValueError()
        if not all(private_path(path, stat.S_ISREG) for path in (config, identity, known)):
            raise ValueError()
        with config.open() as stream:
            contents = stream.read(4097)
        with known.open() as stream:
            pin = stream.read(1025)
        lines = contents.splitlines()
        marker = '# Safe Code host bridge ' + client_id
        if not lines or lines[0] != marker or len(contents) > 4096:
            raise ValueError()
        fields = {}
        for line in lines[1:]:
            key, value = line.strip().split(None, 1)
            if key in fields:
                raise ValueError()
            fields[key] = value
        alias = 'safe-code-host-' + client_id
        if (fields.get('HostName') != '127.0.0.1' or fields.get('IdentityFile') != str(identity)
                or fields.get('UserKnownHostsFile') != str(known) or fields.get('HostKeyAlias') != alias
                or not 1024 <= int(fields['Port']) <= 65535
                or not re.fullmatch(r'[a-zA-Z0-9_.@\\-]{1,128}', fields['User'])
                or not re.fullmatch(re.escape(alias) + r' ssh-ed25519 [A-Za-z0-9+/=]+\n?', pin)):
            raise ValueError()
    except (KeyError, OSError, ValueError):
        raise ToolError('Enable or reconnect SafeCode 本機遠端控制 on the selected Mac.') from None
    # Use only validated fields from SafeCode's connection. Do not run directives
    # from an SSH config or use a connection agent, inherited proxy, or fallback.
    return [
        '/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', fields['Port'], '-i', str(identity), '-l', fields['User'],
        '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + alias,
        '-o', 'UserKnownHostsFile=' + str(known), '-o', 'GlobalKnownHostsFile=/dev/null',
        '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
        '-o', 'ConnectTimeout=8', '--', '127.0.0.1', REMOTE_COMMAND,
    ]


def exchange_ssh(args, body, *, timeout=SSH_TIMEOUT):
    """Transfer a bounded request and response with a single operation deadline."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            with selectors.DefaultSelector() as channels:
                channels.register(proc.stdin, selectors.EVENT_WRITE)
                channels.register(proc.stdout, selectors.EVENT_READ)
                for pipe in (proc.stdin, proc.stdout):
                    os.set_blocking(pipe.fileno(), False)
                sent = 0
                output = bytearray()
                while channels.get_map():
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        raise ToolError('The Mac file tool timed out.')
                    for key, event in channels.select(wait):
                        pipe = key.fileobj
                        if event == selectors.EVENT_WRITE:
                            sent += os.write(pipe.fileno(), body[sent:sent + 16384])
                            if sent == len(body):
                                channels.unregister(pipe)
                                pipe.close()
                        else:
                            data = os.read(pipe.fileno(), 16384)
                            if not data:
                                channels.unregister(pipe)
                            output.extend(data)
                            if len(output) > MAX_FRAME:
                                raise ToolError('The Mac file tool response is too large.')
            if proc.wait(timeout=max(0, deadline - time.monotonic())):
                raise ToolError('The Mac file tool could not run. Check SafeCode 本機遠端控制 and Python 3 on the Mac.')
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        finally:
            if proc.poll() is None:
                proc.kill()


def call_tool(client_id, workspace, name, arguments):
    tool = TOOL_DEFINITIONS.get(name) if isinstance(name, str) else None
    if not tool or not isinstance(arguments, dict) or arguments.keys() != tool[1].keys():
        raise ToolError('Invalid Mac file tool arguments.')
    for key, val in arguments.items():
        limit = MAX_TEXT if key == 'text' else 4096
        if not isinstance(val, str) or len(val.encode('utf-8')) > limit:
            raise ToolError('Invalid or oversized Mac file tool argument.')
    body = json.dumps({'name': name, 'workspace': workspace, 'arguments': arguments}).encode()
    if len(body) > MAX_FRAME:
        raise ToolError('The Mac file tool request is too large.')
    try:
        return exchange_ssh(bridge_arguments(client_id), body)
    except subprocess.TimeoutExpired:
        raise ToolError('The Mac file tool timed out.') from None
    except (OSError, ValueError):
        raise ToolError('The Mac file tool could not run. Check SafeCode 本機遠端控制 and Python 3 on the Mac.') from None


def mac_workspace_path(value):
    return (isinstance(value, str) and value.startswith('/') and '\x00' not in value
            and all(part not in ('', '.', '..') for part in value[1:].split('/'))
            and len(value.encode('utf-8')) <= 4096)


def managed_workspace_allowed(client_id, workspace):
    path = Path.home()/'.config/safe-code/cloudcli-gateway.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return False
            data = stream.read(16385)
        if len(data) > 16384:
            return False
        host = json.loads(data)['hostControl']
        roots = host.get('projectWorkspaces', [host['workspace']])
        return (host['clientId'] == client_id and isinstance(roots, list) and mac_workspace_path(workspace)
                and any(mac_workspace_path(root) and Path(workspace).is_relative_to(root) for root in roots))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def dispatch(request, client_id, workspace, *, managed_project=False):
    method = request.get('method')
    params = request.get('params') or {}
    if not isinstance(params, dict):
        raise ToolError('Invalid request parameters.')
    if method == 'initialize':
        version = params.get('protocolVersion')
        return {
            'protocolVersion': version if isinstance(version, str) and version in PROTOCOL_VERSIONS else '2025-06-18',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'safecode-host-control', 'version': '1.0.0'},
        }
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [
            {'name': name, 'description': description, 'inputSchema': {
                'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False,
            }} for name, (description, fields) in TOOL_DEFINITIONS.items()
        ]}
    if method == 'tools/call':
        try:
            if managed_project and not managed_workspace_allowed(client_id, workspace):
                raise ToolError('The SafeCode workspace authorization for this project is no longer active.')
            result = call_tool(client_id, workspace, params.get('name'), params.get('arguments'))
        except (ToolError, UnicodeError) as error:
            result = {'error': str(error) if isinstance(error, ToolError) else 'The tool request is not valid UTF-8.'}
        if 'error' in result:
            return {'isError': True, 'content': [{'type': 'text', 'text': result['error']}]}
        text = result.get('text') if params.get('name') == 'read_text_file' else json.dumps(result, ensure_ascii=False)
        if not isinstance(text, str):
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Invalid Mac file tool response.'}]}
        return {'content': [{'type': 'text', 'text': text}]}
    raise NotImplementedError()


def serve(client_id, workspace, *, managed_project=False):
    while line := sys.stdin.buffer.readline(MAX_FRAME + 1):
        if len(line) > MAX_FRAME:
            # Stop instead of treating part of an oversized line as a new request.
            break
        request_id = None
        result = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                raise ToolError('Invalid JSON-RPC request.')
            if 'id' not in request:
                continue
            request_id = request['id']
            if request_id is not None and type(request_id) not in (str, int):
                request_id = None
                raise ToolError('Invalid JSON-RPC request id.')
            result['result'] = dispatch(request, client_id, workspace, managed_project=managed_project)
        except (ValueError, UnicodeError):
            result['error'] = {'code': -32700, 'message': 'Invalid JSON input.'}
        except NotImplementedError:
            result['error'] = {'code': -32601, 'message': 'Method not found.'}
        except ToolError as error:
            result['error'] = {'code': -32600, 'message': str(error)}
        print(json.dumps({'jsonrpc': '2.0', 'id': request_id, **result}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--workspace')
    parser.add_argument('--resolve-desktop-session', metavar='UUID')
    parser.add_argument('--check', action='store_true', help='Verify the authorized Mac workspace with a read-only request')
    parser.add_argument('--managed-project', action='store_true', help='Require the current SafeCode workspace binding')
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{20}', args.client_id):
        parser.error('invalid SafeCode host control identifier')
    if args.resolve_desktop_session is not None:
        try:
            body = json.dumps({'name': 'desktop_workspace', 'session': args.resolve_desktop_session}).encode()
            print(json.dumps(exchange_ssh(bridge_arguments(args.client_id), body, timeout=7)))
        except (ToolError, OSError, ValueError, subprocess.TimeoutExpired):
            parser.exit(1, 'SafeCode could not resolve the Mac desktop session.\n')
        return
    if not mac_workspace_path(args.workspace):
        parser.error('workspace must be an absolute Mac directory')
    if args.check:
        if args.managed_project and not managed_workspace_allowed(args.client_id, args.workspace):
            parser.exit(1, 'The SafeCode workspace authorization for this project is no longer active.\n')
        try:
            outcome = call_tool(args.client_id, args.workspace, 'list_files', {'path': '.'})
        except ToolError as error:
            parser.exit(1, str(error) + '\n')
        if 'error' in outcome:
            parser.exit(1, outcome['error'] + '\n')
        print(json.dumps({'ready': True}))
    else:
        serve(args.client_id, args.workspace, managed_project=args.managed_project)


if __name__ == '__main__':
    main()
