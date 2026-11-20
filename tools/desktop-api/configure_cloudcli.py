#!/usr/bin/env python3
"""Bind a CloudCLI project to the existing SafeCode Mac control tunnel."""

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from host_control_mcp import ToolError, bridge_arguments, call_tool, mac_workspace_path


COMPONENT = '/opt/safe-code/desktop-api/host_control_mcp.py'
SERVER_NAME = 'safecode_host'
MODEL_ID = re.compile(r'claude-[a-z0-9]+(?:-[a-z0-9]+)*')
CLIENT_ID = re.compile(r'[a-f0-9]{20}')
MAX_SETTINGS_BYTES = 16 * 1024 * 1024


def read_object(path, *, allow_missing=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if allow_missing:
            return None, {}
        raise
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError('Configuration needs to be a regular file owned by the working account.')
        original = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(original) > MAX_SETTINGS_BYTES:
        raise ValueError('CloudCLI configuration exceeds the supported file size.')
    data = json.loads(original)
    if not isinstance(data, dict):
        raise ValueError('CloudCLI configuration must contain a JSON object.')
    return original, data


def atomic_settings(path, value, expected):
    original, _ = read_object(path, allow_missing=True)
    if original != expected:
        raise ValueError('Configuration changed during setup. Run the setup again.')
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', delete=False) as target:
        staged = Path(target.name)
        try:
            os.fchmod(target.fileno(), 0o600)
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def mcp_entry(target):
    roots = target.get('projectWorkspaces')
    scope = [roots[0], '--managed-project'] if roots else [target['workspace']]
    return {
        'type': 'stdio',
        'command': '/usr/bin/python3',
        'args': ['-I', '-B', COMPONENT, '--client-id', target['clientId'], '--workspace', *scope],
    }


def project_servers(settings, project, *, create=False):
    projects = settings.setdefault('claudeProjects', {}) if create else settings.get('claudeProjects', {})
    entry = projects.setdefault(project, {}) if create else projects.get(project, {})
    return entry.setdefault('mcpServers', {}) if create else entry.get('mcpServers', {})


def select_host(home, requested):
    if requested is not None:
        bridge_arguments(requested)
        return requested
    active = []
    for path in (home/'.ssh/safe-code-host').glob('*'):
        if CLIENT_ID.fullmatch(path.name):
            try:
                bridge_arguments(path.name)
            except ToolError:
                continue
            active.append(path.name)
    if len(active) != 1:
        raise ValueError('Enable SafeCode 本机远程控制. If several clients are configured, use --client-id to select one.')
    return active[0]


@contextmanager
def setup_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = directory.lstat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077 or not stat.S_ISDIR(mode.st_mode):
        raise ValueError('The SafeCode configuration directory needs to be private to the working account.')
    fd = os.open(directory/'cloudcli-gateway.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def configure(args, home, config_path):
    original_config, old_config = read_object(config_path, allow_missing=True)
    default_project = home/'.local/share/safe-code/cloudcli-workspace'
    project = args.project or default_project
    if not project.is_absolute():
        raise ValueError('Use an absolute CloudCLI project directory on the VPS.')
    if args.project is None:
        project.mkdir(parents=True, mode=0o700, exist_ok=True)
    project = project.resolve(strict=True)
    if not project.is_dir() or not os.access(project, os.R_OK | os.X_OK):
        raise ValueError('The CloudCLI project directory must be accessible by the working account.')
    roots = list(dict.fromkeys([args.workspace, *args.allow_workspace]))
    if not 1 <= len(roots) <= 16 or any(not mac_workspace_path(root) or len(root) > 512
                                      or any(ord(char) < 32 for char in root) for root in roots):
        raise ValueError('Use an absolute folder on the Mac, such as /Users/example/Desktop.')
    models = list(dict.fromkeys(args.models))
    if not 1 <= args.port <= 65535 or not 1 <= len(models) <= 32 or any(not MODEL_ID.fullmatch(model) or len(model) > 80 for model in models):
        raise ValueError('Provide valid CloudCLI port and explicit Claude model IDs.')
    previous = old_config.get('hostControl')
    prior_project = old_config.get('projectPath')
    client = select_host(home, args.client_id)
    # Retain the previous identity field for session lookup across a workspace reconfiguration.
    legacy_workspace = (previous.get('workspace') if isinstance(previous, dict) and previous.get('clientId') == client
                        and prior_project == str(project) else None)
    target = {'clientId': client, 'workspace': legacy_workspace or roots[0], 'projectWorkspaces': roots}
    if args.allow_ssh_shell:
        target['allowSshShell'] = True
    if args.device_name:
        if len(args.device_name) > 100 or any(ord(char) < 32 for char in args.device_name):
            raise ValueError('Use a short device name.')
        target['deviceName'] = args.device_name
    for root in roots:
        result = call_tool(client, root, 'list_files', {'path': '.'})
        if not isinstance(result.get('entries'), list):
            raise ValueError('The existing Mac connection could not read a selected workspace.')
    key = getpass.getpass('CloudCLI API Key: ').strip() or old_config.get('apiKey', '')
    if not isinstance(key, str) or not re.fullmatch(r'[\x21-\x7e]{16,512}', key):
        raise ValueError('Enter the CloudCLI API Key generated in CloudCLI settings.')
    config = {'apiKey': key, 'port': args.port, 'projectPath': str(project), 'models': models, 'hostControl': target}
    claude_path = home/'.claude.json'
    original_claude, claude = read_object(claude_path)
    servers = project_servers(claude, str(project), create=True)
    expected = mcp_entry(target)
    current = servers.get(SERVER_NAME)
    is_old = prior_project == str(project) and isinstance(previous, dict) and current == mcp_entry(previous)
    if current is not None and current != expected and not is_old:
        raise ValueError('This CloudCLI project already has a different server named safecode_host.')
    if prior_project and prior_project != str(project) and isinstance(previous, dict):
        prior_servers = project_servers(claude, prior_project)
        if prior_servers.get(SERVER_NAME) == mcp_entry(previous):
            del prior_servers[SERVER_NAME]
    if args.allow_ssh_shell:
        servers.pop(SERVER_NAME, None)
    else:
        servers[SERVER_NAME] = expected
    atomic_settings(claude_path, claude, original_claude)
    try:
        atomic_settings(config_path, config, original_config)
    except Exception:
        # Restore only our own unchanged edit if the gateway settings failed.
        if original_claude is not None:
            new_original, new_claude = read_object(claude_path)
            if new_claude == claude:
                atomic_settings(claude_path, json.loads(original_claude), new_original)
        raise


def disable(home, config_path):
    original_config, config = read_object(config_path, allow_missing=True)
    if original_config is None:
        return
    target = config.get('hostControl')
    project = config.get('projectPath')
    claude_path = home/'.claude.json'
    if isinstance(target, dict) and isinstance(project, str):
        original, claude = read_object(claude_path, allow_missing=True)
        servers = project_servers(claude, project)
        if servers.get(SERVER_NAME) == mcp_entry(target):
            del servers[SERVER_NAME]
            atomic_settings(claude_path, claude, original)
    if read_object(config_path)[0] != original_config:
        raise ValueError('Gateway configuration changed during setup.')
    config_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disable', action='store_true', help='remove the CloudCLI binding')
    parser.add_argument('--project', type=Path, help='CloudCLI project directory on the VPS')
    parser.add_argument('--workspace', help='absolute path to the selected folder on the Mac')
    parser.add_argument('--allow-workspace', action='append', default=[], help='additional authorized Mac folder, repeat as needed')
    parser.add_argument('--allow-ssh-shell', action='store_true',
                        help='use the existing SafeCode SSH connection for authorized Mac projects')
    parser.add_argument('--device-name', help='display label for the selected Mac')
    parser.add_argument('--client-id', help='the existing SafeCode host control ID, if several exist')
    parser.add_argument('--port', type=int, default=3001, help='CloudCLI loopback listener port')
    parser.add_argument('--model', dest='models', action='append', help='allowed Claude model ID; repeat for several')
    args = parser.parse_args()
    if not os.getuid():
        parser.error('run this command as the CloudCLI working account')
    if not args.disable and not (args.workspace and args.models):
        parser.error('--workspace and at least one --model are required')
    home = Path.home()
    config_path = home/'.config/safe-code/cloudcli-gateway.json'
    try:
        with setup_lock(config_path.parent):
            if args.disable:
                disable(home, config_path)
            else:
                configure(args, home, config_path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ToolError) as error:
        # Do not emit JSON decoder input, secrets, paths, or subprocess output.
        parser.exit(1, (str(error) if type(error) in (ValueError, ToolError) else 'Unable to update the CloudCLI gateway configuration.')+'\n')
    print('CloudCLI backend disabled. Reopen or restart the SafeCode gateway to apply.' if args.disable
          else 'CloudCLI backend configured. Reopen or restart the SafeCode gateway to apply.')


if __name__ == '__main__':
    main()
