#!/usr/bin/env python3
"""Isolated DeepSeek configuration for Claude Code CLI and Safe Code's CloudCLI.

This tool is intentionally independent from the Safe Code product source.  It
configures an existing Ubuntu test VM so that:

* the CloudCLI systemd service gets a per-service ``EnvironmentFile`` containing
  DeepSeek's Anthropic-compatible endpoint and the user's personal API key;
* the same user gets a ``claude-deepseek`` wrapper for the terminal, without
  replacing the official ``claude`` binary or writing a fake OAuth login.

It never writes OAuth credentials and never prints the API key.  The key is
accepted interactively (hidden), from a 0600 key file, from a 0600 env file, or
from stdin.  All generated secret material stays in a 0600 file; backups are
root-only 0600 files under ``/var/backups/safe-code-deepseek``.

Run ``--help`` for commands.  The install/restore commands require root because
they modify a systemd drop-in and a different user's home directory.  This file
does not connect to any remote host by itself.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

SERVICE_NAME = "safe-code-cloudcli"
UNIT_PATH = Path("/etc/systemd/system") / (SERVICE_NAME + ".service")
DROPIN_DIR = Path("/etc/systemd/system") / (SERVICE_NAME + ".service.d")
DROPIN_PATH = DROPIN_DIR / "50-deepseek.conf"
STATE_ROOT = Path("/var/lib/safe-code-deepseek")
BACKUP_ROOT = Path("/var/backups/safe-code-deepseek")

OFFICIAL_BASE_URL = "https://api.deepseek.com/anthropic"
OFFICIAL_PROFILE = {
    "main": "deepseek-flash[1m]",
    "opus": "deepseek-flash[1m]",
    "sonnet": "deepseek-flash[1m]",
    "haiku": "deepseek-flash",
    "subagent": "deepseek-flash",
}
PRO_PROFILE = dict(OFFICIAL_PROFILE, opus="deepseek-v4-pro")

# These are the current official DeepSeek model names documented for Claude
# Code.  The legacy names are accepted for labs that still have access to them.
MODEL_RE = re.compile(r"^deepseek-(?:flash(?:\[1m\])?|v4-pro|chat|reasoner)$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+/-]{15,511}$")
UNIT_USER_RE = re.compile(r"^\s*User\s*=\s*(\S+)\s*$", re.MULTILINE)
ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}
MARKER = "# Managed by Safe Code DeepSeek isolated test"
WRAPPER_MARKER = "# safe-code-deepseek-wrapper-v1"


class ToolError(RuntimeError):
    """A user-facing error whose message never contains the API key."""


@dataclass
class Paths:
    user: str
    uid: int
    gid: int
    home: Path
    env: Path
    wrapper: Path
    dropin: Path
    unit: Path
    state: Path
    backup_root: Path

    @property
    def state_dir(self) -> Path:
        return self.state.parent


def utc_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_regular_file(path: Path, limit: int = 1_000_000) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        handle = os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise
    with handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ToolError("文件过大，拒绝读取：%s" % path)
    return data


def parse_env_text(text: str) -> Dict[str, str]:
    if "\x00" in text:
        raise ToolError("环境文件包含 NUL 字节，拒绝使用。")
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ToolError("环境文件行格式应为 KEY=VALUE，拒绝使用。")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ToolError("环境文件包含不支持的键名：%s" % key)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in values and values[key] != value:
            raise ToolError("环境文件重复定义了不同的键：%s" % key)
        values[key] = value
    return values


def validate_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise ToolError("API key 为空。")
    if not KEY_RE.fullmatch(key):
        raise ToolError(
            "API key 格式不受支持：只接受 16-512 个字母、数字和 . _ ~ + / -，"
            "不能包含空白、引号、等号或换行。"
        )
    lowered = key.lower()
    for marker in ("your", "example", "changeme", "placeholder", "replace", "<", ">"):
        if marker in lowered:
            raise ToolError("这看起来是占位符，不是个人 DeepSeek API key。")
    return key


def read_key_file(path: Path, kind: str, allowed_uids: Sequence[int]) -> str:
    if path.is_symlink():
        raise ToolError("拒绝使用符号链接形式的密钥文件：%s" % path)
    try:
        info = path.stat()
    except OSError as exc:
        raise ToolError("无法读取密钥文件 %s：%s" % (path, exc.strerror or exc))
    if not stat.S_ISREG(info.st_mode):
        raise ToolError("密钥文件必须是普通文件：%s" % path)
    if info.st_size > 65536:
        raise ToolError("密钥文件过大：%s" % path)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ToolError("密钥文件权限过宽，请先执行 chmod 600 %s" % path)
    if os.geteuid() != 0 and info.st_uid not in allowed_uids:
        raise ToolError("密钥文件必须属于当前用户：%s" % path)
    try:
        text = read_regular_file(path, 65536).decode("utf-8", "strict")
    except OSError as exc:
        raise ToolError("无法读取密钥文件 %s：%s" % (path, exc.strerror or exc))
    except UnicodeError:
        raise ToolError("密钥文件不是有效的 UTF-8 文本：%s" % path)

    if kind == "key":
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(lines) != 1:
            raise ToolError("密钥文件必须只包含一行 API key。")
        return validate_key(lines[0].strip("'\""))

    values = parse_env_text(text)
    unknown = sorted(set(values) - {"ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY"})
    if unknown:
        raise ToolError(
            "输入环境文件只允许 ANTHROPIC_AUTH_TOKEN 或 DEEPSEEK_API_KEY；"
            "不支持的键：%s" % ", ".join(unknown)
        )
    present = [name for name in ("ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY") if values.get(name)]
    if len(present) != 1:
        raise ToolError("输入环境文件必须且只能提供一个个人 API key。")
    return validate_key(values[present[0]])


def read_secret(args: argparse.Namespace) -> str:
    sources = [
        bool(getattr(args, "key_file", None)),
        bool(getattr(args, "env_file", None)),
        bool(getattr(args, "key_stdin", False)),
    ]
    if sum(sources) > 1:
        raise ToolError("--key-file、--env-file 和 --key-stdin 只能选择一个。")

    allowed_uids = [os.geteuid()]
    if os.geteuid() == 0:
        allowed_uids.append(0)

    if getattr(args, "key_file", None):
        return read_key_file(Path(args.key_file).expanduser(), "key", allowed_uids)
    if getattr(args, "env_file", None):
        return read_key_file(Path(args.env_file).expanduser(), "env", allowed_uids)
    if getattr(args, "key_stdin", False):
        if sys.stdin.isatty():
            raise ToolError("当前是交互终端，请不要使用 --key-stdin；直接运行即可隐藏输入。")
        line = sys.stdin.readline()
        if not line:
            raise ToolError("stdin 没有提供 API key。")
        return validate_key(line.rstrip("\r\n"))

    if sys.stdin.isatty():
        key = getpass.getpass("DeepSeek 个人 API key（输入不显示）: ")
        return validate_key(key)

    raise ToolError(
        "未提供个人 DeepSeek API key。请在交互终端运行以隐藏输入，"
        "或用 chmod 600 的 --key-file / --env-file 提供。"
        "不要使用公司 API key、Harness 网页凭据或 Claude OAuth 凭据。"
        "需要用户提供：个人 DeepSeek API key（platform.deepseek.com）。"
    )


def validate_model(model: str, label: str) -> str:
    model = (model or "").strip()
    if not MODEL_RE.fullmatch(model):
        raise ToolError(
            "%s 模型名不受支持：%s（允许 deepseek-flash、deepseek-flash[1m]、"
            "deepseek-v4-pro、deepseek-chat、deepseek-reasoner）" % (label, model)
        )
    return model


def resolve_models(args: argparse.Namespace) -> Dict[str, str]:
    profile = dict(OFFICIAL_PROFILE if getattr(args, "profile", "official") == "official" else PRO_PROFILE)
    for key in ("main", "opus", "sonnet", "haiku", "subagent"):
        override = getattr(args, key + "_model", None)
        if override:
            profile[key] = validate_model(override, key)
    for key, value in list(profile.items()):
        profile[key] = validate_model(value, key)
    effort = getattr(args, "effort", "max") or "max"
    if effort not in ("low", "medium", "high", "max"):
        raise ToolError("--effort 只能是 low、medium、high 或 max。")
    profile["effort"] = effort
    return profile


def render_env(key: str, models: Mapping[str, str]) -> bytes:
    lines = [
        MARKER,
        "# Contains a secret. Mode 0600. Never commit, paste, or log this file.",
        "ANTHROPIC_BASE_URL=" + OFFICIAL_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN=" + key,
        "ANTHROPIC_MODEL=" + models["main"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL=" + models["opus"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL=" + models["sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL=" + models["haiku"],
        "ANTHROPIC_SMALL_FAST_MODEL=" + models["haiku"],
        "CLAUDE_CODE_SUBAGENT_MODEL=" + models["subagent"],
        "CLAUDE_CODE_EFFORT_LEVEL=" + models["effort"],
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW=786432",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_wrapper() -> bytes:
    script = """#!/bin/sh
{marker}
# Loads only allowlisted values from the 0600 env file, then runs the official
# Claude Code binary.  It does not replace claude and does not write OAuth data.
set -eu
ENV_FILE="${{SAFE_CODE_DEEPSEEK_ENV:-$HOME/.config/safe-code/deepseek-claude.env}}"
CLAUDE_BIN="${{SAFE_CODE_CLAUDE_BIN:-$HOME/.local/bin/claude}}"
if [ ! -f "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
  echo "safe-code-deepseek: 环境文件不存在或不是普通文件: $ENV_FILE" >&2
  exit 1
fi
mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)
if [ "$mode" != "600" ]; then
  echo "safe-code-deepseek: 环境文件权限必须为 600: $ENV_FILE" >&2
  exit 1
fi
while IFS='=' read -r key value; do
  case "$key" in
    ANTHROPIC_*|CLAUDE_CODE_*|DISABLE_*)
      export "$key=$value"
      ;;
  esac
done < "$ENV_FILE"
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "safe-code-deepseek: 未找到可执行 Claude Code: $CLAUDE_BIN" >&2
  exit 1
fi
exec "$CLAUDE_BIN" "$@"
""".format(marker=WRAPPER_MARKER)
    return script.encode("utf-8")


def render_dropin(env_path: Path) -> bytes:
    text = """{marker}
[Service]
EnvironmentFile={env}
UnsetEnvironment=ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
LimitCORE=0
""".format(marker=MARKER, env=str(env_path))
    return text.encode("utf-8")


def parse_unit_user(unit_path: Path) -> Optional[str]:
    if not unit_path.exists():
        return None
    try:
        text = unit_path.read_text("utf-8", errors="replace")
    except OSError as exc:
        raise ToolError("无法读取 systemd 单元 %s：%s" % (unit_path, exc))
    matches = UNIT_USER_RE.findall(text)
    if not matches:
        return None
    user = matches[-1].strip()
    return user or None


def resolve_paths(
    user_arg: Optional[str],
    *,
    require_service: bool,
    require_root: bool,
    unit_path: Path = UNIT_PATH,
    dropin_path: Path = DROPIN_PATH,
    state_path: Optional[Path] = None,
    backup_root: Path = BACKUP_ROOT,
) -> Paths:
    if require_root and os.geteuid() != 0:
        raise ToolError("此操作需要 root；请使用 sudo python3 <本脚本> ...")

    unit_user = parse_unit_user(unit_path) if unit_path.exists() else None
    current_user = pwd.getpwuid(os.geteuid()).pw_name
    if user_arg:
        user = user_arg
    elif os.geteuid() == 0 and unit_user:
        user = unit_user
    else:
        user = current_user

    if require_service:
        if not unit_path.exists():
            raise ToolError(
                "未找到 %s。请先在隔离 VM 中安装 Safe Code CloudCLI，"
                "或加 --skip-service 只配置终端 Claude Code。" % unit_path
            )
        if unit_user and user != unit_user:
            raise ToolError(
                "CloudCLI 服务运行用户是 %s，但指定了 %s；"
                "请使用 --user %s 或先停止/重装 CloudCLI。" % (unit_user, user, unit_user)
            )
    if os.geteuid() != 0 and user != current_user:
        raise ToolError("非 root 用户只能配置自己的账户。")

    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        raise ToolError("系统没有用户：%s" % user)

    home = Path(pw.pw_dir).resolve()
    if not home.is_absolute() or not home.is_dir():
        raise ToolError("用户主目录不存在或无效：%s" % home)

    env_path = home / ".config" / "safe-code" / "deepseek-claude.env"
    for value in (str(env_path), str(home)):
        if any(ch in value for ch in (" ", "\t", "\n", "\r", '"', "'", "\\", "%")):
            raise ToolError(
                "主目录路径含有 systemd EnvironmentFile 不便安全处理的字符：%s" % value
            )

    state_path = state_path or (STATE_ROOT / "state.json")
    return Paths(
        user=user,
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        home=home,
        env=env_path,
        wrapper=home / ".local" / "bin" / "claude-deepseek",
        dropin=dropin_path,
        unit=unit_path,
        state=state_path,
        backup_root=backup_root,
    )


def ensure_root_dir(path: Path, mode: int) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ToolError("目录路径不安全：%s" % path)
        if os.geteuid() == 0:
            os.chmod(path, mode)
            os.chown(path, 0, 0)
        return
    path.mkdir(parents=True, mode=mode)
    os.chmod(path, mode)
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def ensure_dir_under(home: Path, target: Path, uid: int, gid: int, final_mode: int) -> None:
    home = home.resolve()
    try:
        relative = target.resolve(strict=False).relative_to(home)
    except ValueError:
        raise ToolError("拒绝在用户主目录之外写入：%s" % target)
    current = home
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        is_last = index == len(parts) - 1
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ToolError("拒绝穿过符号链接写入：%s" % current)
            if not current.is_dir():
                raise ToolError("路径不是目录：%s" % current)
            if is_last and os.geteuid() == 0:
                os.chmod(current, final_mode)
                os.chown(current, uid, gid)
        else:
            mode = final_mode if is_last else 0o755
            current.mkdir(mode=mode)
            os.chmod(current, mode)
            if os.geteuid() == 0:
                os.chown(current, uid, gid)


def atomic_write(path: Path, data: bytes, mode: int, uid: Optional[int], gid: Optional[int]) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ToolError("目标目录不安全：%s" % parent)
    if path.is_symlink():
        raise ToolError("拒绝写入符号链接：%s" % path)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        if os.geteuid() == 0 and uid is not None and gid is not None:
            os.chown(temp_path, uid, gid)
        os.replace(temp_path, path)
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def validate_managed_file(path: Path, role: str, force: bool) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise ToolError("拒绝覆盖符号链接：%s" % path)
    if not path.is_file():
        raise ToolError("目标不是普通文件：%s" % path)
    if force:
        return
    if role == "env":
        try:
            values = parse_env_text(path.read_text("utf-8"))
        except (OSError, UnicodeError, ToolError) as exc:
            raise ToolError("现有环境文件无法解析，未覆盖：%s（%s）" % (path, exc))
        unknown = sorted(set(values) - ENV_KEYS)
        if unknown:
            raise ToolError(
                "现有环境文件含非本工具管理的键：%s；"
                "为避免覆盖其他配置已停止。确认后使用 --force。" % ", ".join(unknown)
            )
        return
    try:
        text = path.read_text("utf-8", errors="replace")[:4096]
    except OSError as exc:
        raise ToolError("无法读取现有文件 %s：%s" % (path, exc))
    expected_marker = WRAPPER_MARKER if role == "wrapper" else MARKER
    if expected_marker not in text:
        raise ToolError(
            "现有文件不是本工具创建的：%s；为避免覆盖已停止。确认后使用 --force。" % path
        )


def load_state(path: Path) -> Optional[Dict[str, object]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("状态文件损坏，拒绝继续：%s（%s）" % (path, exc))
    if not isinstance(value, dict):
        raise ToolError("状态文件格式不正确：%s" % path)
    return value


def save_state(path: Path, state: Dict[str, object]) -> None:
    ensure_root_dir(path.parent, 0o700)
    state["updatedAt"] = utc_now()
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    atomic_write(path, data, 0o600, 0, 0)


def unique_backup_dir(root: Path) -> Path:
    ensure_root_dir(root, 0o700)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    candidate = root / stamp
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = root / (stamp + "-" + str(suffix))
    candidate.mkdir(mode=0o700)
    os.chmod(candidate, 0o700)
    if os.geteuid() == 0:
        os.chown(candidate, 0, 0)
    return candidate


def create_backup(paths: Paths, roles: Mapping[str, Path]) -> Dict[str, object]:
    backup_dir = unique_backup_dir(paths.backup_root)
    records: Dict[str, Dict[str, object]] = {}
    for role, path in roles.items():
        record: Dict[str, object] = {
            "path": str(path),
            "existed": False,
            "backup": None,
            "mode": None,
            "uid": None,
            "gid": None,
            "sha256": None,
        }
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ToolError("拒绝备份符号链接：%s" % path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("拒绝备份非普通文件：%s" % path)
            data = read_regular_file(path)
            destination = backup_dir / role
            atomic_write(destination, data, 0o600, 0, 0)
            record.update(
                existed=True,
                backup=str(destination),
                mode=stat.S_IMODE(info.st_mode),
                uid=info.st_uid,
                gid=info.st_gid,
                sha256=sha256_bytes(data),
            )
        records[role] = record

    manifest = {
        "version": 1,
        "createdAt": utc_now(),
        "targetUser": paths.user,
        "backupDir": str(backup_dir),
        "files": records,
    }
    manifest_path = backup_dir / "manifest.json"
    atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        0o600,
        0,
        0,
    )
    return {"backupDir": str(backup_dir), "manifest": str(manifest_path), "files": records}


def write_managed_files(paths: Paths, key: str, models: Mapping[str, str]) -> Dict[str, str]:
    ensure_dir_under(paths.home, paths.env.parent, paths.uid, paths.gid, 0o700)
    ensure_dir_under(paths.home, paths.wrapper.parent, paths.uid, paths.gid, 0o755)
    ensure_root_dir(paths.dropin.parent, 0o755)

    env_data = render_env(key, models)
    wrapper_data = render_wrapper()
    dropin_data = render_dropin(paths.env)

    atomic_write(paths.env, env_data, 0o600, paths.uid, paths.gid)
    atomic_write(paths.wrapper, wrapper_data, 0o755, paths.uid, paths.gid)
    atomic_write(paths.dropin, dropin_data, 0o644, 0, 0)
    return {
        "env": sha256_bytes(env_data),
        "wrapper": sha256_bytes(wrapper_data),
        "dropin": sha256_bytes(dropin_data),
    }


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_command(["systemctl"] + list(args), check=check, timeout=45)


def run_command(
    command: Sequence[str],
    *,
    check: bool = True,
    timeout: int = 60,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError:
        raise ToolError("找不到命令：%s" % command[0])
    except subprocess.TimeoutExpired:
        raise ToolError("命令超时：%s %s" % (command[0], command[1] if len(command) > 1 else ""))
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ToolError(
            "命令失败：%s %s（退出码 %s）%s"
            % (
                command[0],
                command[1] if len(command) > 1 else "",
                result.returncode,
                (": " + detail[:500]) if detail else "",
            )
        )
    return result


def service_is_active(systemctl_fn=systemctl) -> bool:
    result = systemctl_fn("is-active", SERVICE_NAME, check=False)
    return result.stdout.strip() == "active"


def service_environment_files(systemctl_fn=systemctl) -> str:
    result = systemctl_fn("show", "-p", "EnvironmentFiles", "--value", SERVICE_NAME, check=False)
    return result.stdout.strip()


def service_environment_key_names(systemctl_fn=systemctl) -> List[str]:
    result = systemctl_fn("show", "-p", "Environment", "--value", SERVICE_NAME, check=False)
    names = set(re.findall(r"(?:^|\s)([A-Z][A-Z0-9_]*)=", result.stdout))
    return sorted(name for name in names if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_")))


def restart_service(systemctl_fn=systemctl, *, health: bool = True) -> bool:
    systemctl_fn("daemon-reload", check=True)
    systemctl_fn("restart", SERVICE_NAME, check=True)
    for _ in range(20):
        if service_is_active(systemctl_fn):
            if health:
                wait_for_health()
            return True
        time.sleep(0.5)
    raise ToolError(
        "CloudCLI 服务重启后未进入 active。请检查 sudo journalctl -u %s；"
        "如需回滚，运行本脚本的 restore 命令。" % SERVICE_NAME
    )


def check_health(timeout: float = 2.0) -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:3001/health", timeout=timeout) as response:
            payload = response.read(4096)
        data = json.loads(payload.decode("utf-8", "replace"))
        if data.get("status") != "ok":
            raise ToolError("CloudCLI /health 未返回 ok。")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("CloudCLI 已启动，但本机 /health 检查失败：%s" % exc)


def wait_for_health(
    total_timeout: float = 30.0,
    interval: float = 0.5,
    connect_timeout: float = 2.0,
    *,
    health_fn=check_health,
    clock_fn=time.monotonic,
    sleep_fn=time.sleep,
) -> None:
    """Wait for /health after systemd reports active.

    systemd considers the service started before CloudCLI's HTTP socket is
    listening, especially on the first start after a restart.  Retry within a
    fixed budget so a real startup failure is still reported promptly.
    """
    deadline = clock_fn() + max(0.0, total_timeout)
    last_error = None
    attempts = 0
    while True:
        remaining = deadline - clock_fn()
        if remaining <= 0:
            break
        attempts += 1
        try:
            health_fn(timeout=min(connect_timeout, remaining))
            return
        except Exception as exc:
            last_error = exc
        remaining = deadline - clock_fn()
        if remaining <= 0:
            break
        sleep_fn(min(interval, remaining))
    raise ToolError(
        "CloudCLI 已 active，但 /health 在约 %.0f 秒内未就绪（尝试 %d 次）：%s"
        % (total_timeout, attempts, last_error)
    )


def apply_config(
    paths: Paths,
    key: str,
    models: Mapping[str, str],
    *,
    profile: str,
    force: bool = False,
    restart: bool = True,
    health: bool = True,
    dry_run: bool = False,
    systemctl_fn=systemctl,
) -> Dict[str, object]:
    if not dry_run:
        validate_key(key)
    roles = {"env": paths.env, "wrapper": paths.wrapper, "dropin": paths.dropin}
    for role, path in roles.items():
        validate_managed_file(path, role, force)

    env_data = render_env(key, models)
    wrapper_data = render_wrapper()
    dropin_data = render_dropin(paths.env)
    desired = {
        "env": sha256_bytes(env_data),
        "wrapper": sha256_bytes(wrapper_data),
        "dropin": sha256_bytes(dropin_data),
    }
    before = {
        role: (sha256_file(path) if path.exists() and not path.is_symlink() else None)
        for role, path in roles.items()
    }

    if dry_run:
        return {
            "dryRun": True,
            "paths": {role: str(path) for role, path in roles.items()},
            "hashes": desired,
            "wouldChange": {role: before[role] != desired[role] for role in roles},
        }

    state = load_state(paths.state)
    if state and state.get("targetUser") != paths.user:
        raise ToolError(
            "已有安装状态属于用户 %s，不是 %s；拒绝覆盖。"
            % (state.get("targetUser"), paths.user)
        )
    status = state.get("status") if state else None

    active_before = service_is_active(systemctl_fn) if paths.unit.exists() else False
    if status in ("installed", "installing"):
        backup = state.get("backup") if state else None
        if not isinstance(backup, dict) or not Path(str(backup.get("backupDir"))).is_dir():
            raise ToolError("已有安装状态缺少有效备份，拒绝继续；请先人工检查 %s。" % paths.state)
    else:
        backup = create_backup(paths, roles)
        state = {
            "version": 1,
            "status": "installing",
            "createdAt": utc_now(),
            "targetUser": paths.user,
            "uid": paths.uid,
            "home": str(paths.home),
            "baseUrl": OFFICIAL_BASE_URL,
            "backup": backup,
            "files": {
                role: {
                    "path": str(path),
                    "existed": backup["files"][role]["existed"],  # type: ignore[index]
                    "backup": backup["files"][role]["backup"],  # type: ignore[index]
                    "originalSha256": backup["files"][role]["sha256"],  # type: ignore[index]
                    "installedSha256": None,
                    "mode": backup["files"][role]["mode"],  # type: ignore[index]
                    "uid": backup["files"][role]["uid"],  # type: ignore[index]
                    "gid": backup["files"][role]["gid"],  # type: ignore[index]
                }
                for role, path in roles.items()
            },
            "service": {"activeBefore": active_before},
        }
        save_state(paths.state, state)

    assert state is not None
    installed = write_managed_files(paths, key, models)
    state["status"] = "installed"
    state["profile"] = profile
    state["models"] = dict(models)
    state["envFile"] = str(paths.env)
    state["wrapper"] = str(paths.wrapper)
    state["dropin"] = str(paths.dropin)
    state["files"] = {}
    for role, path in roles.items():
        original = backup["files"][role]  # type: ignore[index]
        state["files"][role] = {
            "path": str(path),
            "existed": original["existed"],
            "backup": original["backup"],
            "originalSha256": original["sha256"],
            "installedSha256": installed[role],
            "mode": original["mode"],
            "uid": original["uid"],
            "gid": original["gid"],
        }

    changed = any(before[role] != installed[role] for role in roles)
    listed = str(paths.env) in service_environment_files(systemctl_fn)
    needs_restart = bool(paths.unit.exists()) and restart and (changed or not active_before or not listed)

    state["service"] = {  # type: ignore[assignment]
        "activeBefore": active_before,
        "changed": changed,
        "restarted": False,
        "activeAfter": service_is_active(systemctl_fn),
        "environmentFileListedBefore": listed,
    }
    save_state(paths.state, state)

    if needs_restart:
        restart_service(systemctl_fn, health=health)
        state["service"] = {  # type: ignore[assignment]
            "activeBefore": active_before,
            "changed": changed,
            "restarted": True,
            "activeAfter": True,
            "environmentFileListedBefore": listed,
        }
        save_state(paths.state, state)

    return {"state": state, "changed": changed, "restarted": needs_restart, "hashes": installed}


def restore_config(
    paths: Paths,
    *,
    force: bool = False,
    systemctl_fn=systemctl,
) -> Dict[str, object]:
    state = load_state(paths.state)
    if not state:
        raise ToolError("没有找到安装状态：%s" % paths.state)
    if state.get("targetUser") != paths.user:
        raise ToolError("状态记录的用户与目标用户不一致，拒绝还原。")
    if state.get("status") not in ("installed", "installing"):
        raise ToolError("状态不是已安装，无需还原（%s）。" % state.get("status"))
    files = state.get("files")
    backup = state.get("backup")
    if not isinstance(backup, dict):
        raise ToolError("状态文件缺少备份记录，拒绝还原。")
    if not isinstance(files, dict):
        files = {}

    unit_user = parse_unit_user(paths.unit) if paths.unit.exists() else None
    if unit_user and unit_user != paths.user:
        raise ToolError(
            "CloudCLI 单元运行用户是 %s，与状态用户 %s 不一致，拒绝还原。"
            % (unit_user, paths.user)
        )
    active_now = service_is_active(systemctl_fn) if paths.unit.exists() else False

    roles = {"env": paths.env, "wrapper": paths.wrapper, "dropin": paths.dropin}
    backup_files = backup.get("files")
    if not isinstance(backup_files, dict):
        backup_files = {}
    for role in roles:
        path = roles[role]
        record = files.get(role)
        if not isinstance(record, dict):
            original = backup_files.get(role)
            if not isinstance(original, dict):
                raise ToolError("状态缺少 %s 记录，拒绝还原。" % role)
            record = {
                "path": str(roles[role]),
                "existed": original.get("existed"),
                "backup": original.get("backup"),
                "originalSha256": original.get("sha256"),
                "installedSha256": None,
                "mode": original.get("mode"),
                "uid": original.get("uid"),
                "gid": original.get("gid"),
            }
            files[role] = record
        expected = record.get("installedSha256")
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ToolError("当前路径是符号链接，拒绝还原：%s" % path)
            if not path.is_file():
                raise ToolError("当前路径不是普通文件，拒绝还原：%s" % path)
            if expected and sha256_file(path) != expected and not force:
                raise ToolError(
                    "文件在安装后被修改：%s；为避免丢失改动已停止，确认后使用 --force。" % path
                )

    for role, path in roles.items():
        record = files[role]
        if bool(record.get("existed")):
            backup_path = record.get("backup")
            if not backup_path or not Path(str(backup_path)).is_file():
                raise ToolError("备份文件缺失，无法还原 %s。" % role)

    for role, path in roles.items():
        record = files[role]
        if bool(record.get("existed")):
            backup_path = record.get("backup")
            data = read_regular_file(Path(str(backup_path)))
            mode = int(record.get("mode") or 0o600)
            uid = record.get("uid")
            gid = record.get("gid")
            if role in ("env", "wrapper"):
                ensure_dir_under(paths.home, path.parent, paths.uid, paths.gid,
                                 0o700 if role == "env" else 0o755)
            else:
                ensure_root_dir(path.parent, 0o755)
            atomic_write(path, data, mode, uid if isinstance(uid, int) else None, gid if isinstance(gid, int) else None)
        else:
            if path.exists():
                if path.is_symlink():
                    raise ToolError("拒绝删除符号链接：%s" % path)
                path.unlink()

    try:
        if paths.dropin.parent.exists() and not any(paths.dropin.parent.iterdir()):
            paths.dropin.parent.rmdir()
    except OSError:
        pass

    if paths.unit.exists():
        systemctl_fn("daemon-reload", check=True)
        if active_now or bool(state.get("service", {}).get("activeBefore")):
            restart_service(systemctl_fn, health=False)
            state["service"] = {  # type: ignore[assignment]
                **state.get("service", {}),  # type: ignore[arg-type]
                "restarted": True,
                "activeAfter": True,
            }

    state["status"] = "restored"
    state["restoredAt"] = utc_now()
    save_state(paths.state, state)
    return {"state": state, "backupDir": backup.get("backupDir")}


def collect_status(paths: Paths, systemctl_fn=systemctl) -> Dict[str, object]:
    env_values: Dict[str, str] = {}
    env_error: Optional[str] = None
    env_mode: Optional[int] = None
    env_uid: Optional[int] = None
    env_gid: Optional[int] = None
    if paths.env.exists() or paths.env.is_symlink():
        if paths.env.is_symlink():
            env_error = "环境文件是符号链接"
        else:
            info = paths.env.stat()
            env_mode = stat.S_IMODE(info.st_mode)
            env_uid = info.st_uid
            env_gid = info.st_gid
            try:
                env_values = parse_env_text(paths.env.read_text("utf-8"))
            except (OSError, UnicodeError, ToolError) as exc:
                env_error = str(exc)

    state = None
    state_error = None
    try:
        state = load_state(paths.state)
    except ToolError as exc:
        state_error = str(exc)

    dropin_text = ""
    if paths.dropin.exists() and not paths.dropin.is_symlink():
        try:
            dropin_text = paths.dropin.read_text("utf-8", errors="replace")
        except OSError:
            dropin_text = ""

    unit_exists = paths.unit.exists()
    active = service_is_active(systemctl_fn) if unit_exists else False
    environment_files = service_environment_files(systemctl_fn) if unit_exists else ""
    conflicts = service_environment_key_names(systemctl_fn) if unit_exists else []
    oauth_credentials = (paths.home / ".claude" / ".credentials.json").exists()

    warnings: List[str] = []
    if env_error:
        warnings.append(env_error)
    if env_mode is not None and env_mode != 0o600:
        warnings.append("环境文件权限不是 600")
    if env_uid is not None and env_uid not in (paths.uid, 0):
        warnings.append("环境文件所有者不是目标用户")
    token = env_values.get("ANTHROPIC_AUTH_TOKEN")
    if paths.env.exists() and not token:
        warnings.append("环境文件没有 ANTHROPIC_AUTH_TOKEN")
    if token:
        try:
            validate_key(token)
        except ToolError:
            warnings.append("环境文件中的 ANTHROPIC_AUTH_TOKEN 格式不符合工具要求")
    if paths.env.exists() and env_values.get("ANTHROPIC_BASE_URL") != OFFICIAL_BASE_URL:
        warnings.append("ANTHROPIC_BASE_URL 不是官方 DeepSeek Anthropic 端点")
    if not paths.wrapper.exists():
        warnings.append("claude-deepseek 包装脚本不存在")
    if not paths.dropin.exists():
        warnings.append("systemd drop-in 不存在")
    if unit_exists and str(paths.env) not in environment_files:
        warnings.append("CloudCLI 服务未列出该 EnvironmentFile")
    if conflicts:
        warnings.append("服务基础环境含冲突键（值未读取）：" + ", ".join(conflicts))
    if oauth_credentials:
        warnings.append("检测到 ~/.claude/.credentials.json；未读取，API token 优先且未伪造 OAuth")
    if state_error:
        warnings.append("安装状态不可读：" + state_error)

    return {
        "targetUser": paths.user,
        "home": str(paths.home),
        "envFile": str(paths.env),
        "envExists": paths.env.exists(),
        "envMode": ("%03o" % env_mode) if env_mode is not None else None,
        "envUid": env_uid,
        "envGid": env_gid,
        "keySet": bool(env_values.get("ANTHROPIC_AUTH_TOKEN")),
        "baseUrl": env_values.get("ANTHROPIC_BASE_URL"),
        "models": {
            "main": env_values.get("ANTHROPIC_MODEL"),
            "opus": env_values.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
            "sonnet": env_values.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
            "haiku": env_values.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            "subagent": env_values.get("CLAUDE_CODE_SUBAGENT_MODEL"),
        },
        "wrapper": str(paths.wrapper),
        "wrapperExists": paths.wrapper.exists(),
        "dropin": str(paths.dropin),
        "dropinExists": paths.dropin.exists(),
        "dropinReferencesEnv": str(paths.env) in dropin_text,
        "unitExists": unit_exists,
        "serviceActive": active,
        "environmentFileListed": str(paths.env) in environment_files,
        "environmentKeyConflicts": conflicts,
        "oauthCredentialsPresent": oauth_credentials,
        "stateStatus": state.get("status") if state else None,
        "stateError": state_error,
        "backupDir": (
            state.get("backup", {}).get("backupDir")
            if state and isinstance(state.get("backup"), dict)
            else None
        ),
        "warnings": warnings,
    }


def print_status(status: Mapping[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("目标用户: %s (%s)" % (status["targetUser"], status["home"]))
    print("环境文件: %s" % status["envFile"])
    print("  存在=%s 权限=%s key=%s base_url=%s"
          % (status["envExists"], status["envMode"], "已设置" if status["keySet"] else "缺失",
             status["baseUrl"]))
    models = status["models"]
    print("  模型: main=%s opus=%s sonnet=%s haiku=%s subagent=%s"
          % (models["main"], models["opus"], models["sonnet"], models["haiku"], models["subagent"]))  # type: ignore[index]
    print("CLI 包装: %s（存在=%s）" % (status["wrapper"], status["wrapperExists"]))
    print("systemd drop-in: %s（存在=%s，引用环境文件=%s）"
          % (status["dropin"], status["dropinExists"], status["dropinReferencesEnv"]))
    print("CloudCLI 服务: unit=%s active=%s EnvironmentFile已列出=%s"
          % (status["unitExists"], status["serviceActive"], status["environmentFileListed"]))
    if status["environmentKeyConflicts"]:
        print("服务环境冲突键（仅名称）: %s" % ", ".join(status["environmentKeyConflicts"]))  # type: ignore[arg-type]
    if status["oauthCredentialsPresent"]:
        print("OAuth 凭据文件存在，但本方案不读取、不使用；API token 优先。")
    print("安装状态: %s" % (status["stateStatus"] or "无"))
    if status["backupDir"]:
        print("备份目录: %s" % status["backupDir"])
    for warning in status["warnings"]:  # type: ignore[union-attr]
        print("警告: %s" % warning)


def confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ToolError("非交互环境需要显式加 --yes。")
    answer = input(prompt + " [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise ToolError("已取消，未做修改。")


def print_plan(paths: Paths, models: Mapping[str, str], args: argparse.Namespace) -> None:
    print("DeepSeek 隔离配置计划（不显示 API key）")
    print("  用户: %s (%s)" % (paths.user, paths.home))
    print("  端点: %s" % OFFICIAL_BASE_URL)
    print("  模型: main=%s opus=%s sonnet=%s haiku=%s subagent=%s"
          % (models["main"], models["opus"], models["sonnet"], models["haiku"], models["subagent"]))
    print("  环境文件: %s（0600）" % paths.env)
    print("  CLI 包装: %s（不替换 claude）" % paths.wrapper)
    print("  systemd drop-in: %s" % paths.dropin)
    print("  备份/状态: %s / %s" % (paths.backup_root, paths.state))
    print("  服务: %s" % ("跳过（--skip-service）" if args.skip_service else "重启 safe-code-cloudcli"))
    print("  OAuth: 不写入、不伪造；现有 OAuth 凭据不会被本工具读取。")


def probe_api(paths: Paths, args: argparse.Namespace) -> None:
    if not args.yes:
        raise ToolError("probe-api 会发起真实计费请求；确认后加 --yes。")
    if not paths.env.is_file() or paths.env.is_symlink():
        raise ToolError("环境文件不存在或是符号链接：%s" % paths.env)
    if stat.S_IMODE(paths.env.stat().st_mode) & 0o077:
        raise ToolError("环境文件权限过宽，拒绝发送 key；请先 chmod 600 %s" % paths.env)
    values = parse_env_text(paths.env.read_text("utf-8"))
    key = values.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        raise ToolError("环境文件没有 ANTHROPIC_AUTH_TOKEN。")
    base = values.get("ANTHROPIC_BASE_URL", OFFICIAL_BASE_URL)
    if not base.startswith("https://api.deepseek.com/"):
        raise ToolError("拒绝向非 DeepSeek 官方端点发送个人 key：%s" % base)
    model = args.model or values.get("ANTHROPIC_MODEL", "deepseek-flash")
    model = model.replace("[1m]", "")
    validate_model(model, "probe")
    if int(args.max_tokens) < 1 or int(args.max_tokens) > 8192:
        raise ToolError("--max-tokens 必须在 1-8192 之间。")
    body = json.dumps(
        {
            "model": model,
            "max_tokens": int(args.max_tokens),
            "messages": [{"role": "user", "content": args.prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/messages",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer " + key,
        },
    )
    print("正在向 DeepSeek 发起一次计费探针（模型 %s）…" % model)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = response.read(1_000_000)
        data = json.loads(payload.decode("utf-8", "replace"))
        texts = [
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        print("响应模型: %s" % data.get("model"))
        print("文本: %s" % redact("".join(texts).strip()[:4000], [key]))
        usage = data.get("usage") or {}
        if usage:
            print("用量: input=%s output=%s"
                  % (usage.get("input_tokens"), usage.get("output_tokens")))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        raise ToolError("DeepSeek API 返回 HTTP %s：%s" % (exc.code, redact(detail[:800], [key])))
    except urllib.error.URLError as exc:
        raise ToolError("无法连接 DeepSeek API：%s" % exc.reason)


def probe_cli(paths: Paths, args: argparse.Namespace) -> None:
    if not args.yes:
        raise ToolError("probe-cli 会启动 Claude Code 并发起真实计费请求；确认后加 --yes。")
    if not paths.wrapper.is_file():
        raise ToolError("包装脚本不存在：%s" % paths.wrapper)
    key = ""
    try:
        key = parse_env_text(paths.env.read_text("utf-8")).get("ANTHROPIC_AUTH_TOKEN", "")
    except (OSError, ToolError):
        pass
    command: List[str]
    if os.geteuid() == 0 and paths.user != pwd.getpwuid(0).pw_name:
        runuser = "/sbin/runuser" if Path("/sbin/runuser").exists() else "runuser"
        command = [
            runuser,
            "-u",
            paths.user,
            "--",
            "/usr/bin/env",
            "HOME=" + str(paths.home),
            str(paths.wrapper),
            "-p",
            args.prompt,
            "--output-format",
            "text",
        ]
    else:
        command = [str(paths.wrapper), "-p", args.prompt, "--output-format", "text"]
    print("正在通过 claude-deepseek 发起一次计费探针…")
    try:
        result = run_command(command, check=False, timeout=240, cwd=paths.home)
    except ToolError:
        raise
    output = redact((result.stdout or "").strip(), [key])
    error = redact((result.stderr or "").strip(), [key])
    if output:
        print(output[-4000:])
    if result.returncode != 0:
        raise ToolError("Claude Code 退出码 %s：%s" % (result.returncode, error[-1500:]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为 Claude Code CLI 与 Safe Code CloudCLI 配置 DeepSeek 官方 Anthropic 兼容端点。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  sudo python3 tools/deepseek_claude_cloud.py install\n"
            "  sudo python3 tools/deepseek_claude_cloud.py install --env-file /root/deepseek.env --yes\n"
            "  sudo python3 tools/deepseek_claude_cloud.py status\n"
            "  sudo python3 tools/deepseek_claude_cloud.py restore --yes\n"
            "  sudo python3 tools/deepseek_claude_cloud.py probe-api --yes\n"
        ),
    )
    parser.add_argument("--debug", action="store_true", help="出错时显示 traceback")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="安装或更新隔离配置")
    install.add_argument("--user", help="CloudCLI 工作用户；默认从 systemd 单元读取")
    key_group = install.add_mutually_exclusive_group()
    key_group.add_argument("--key-file", help="只含一行 API key 的 0600 文件")
    key_group.add_argument("--env-file", help="含 ANTHROPIC_AUTH_TOKEN 或 DEEPSEEK_API_KEY 的 0600 文件")
    key_group.add_argument("--key-stdin", action="store_true", help="从 stdin 读取 key（无回显，避免 argv）")
    install.add_argument("--profile", choices=["official", "pro"], default="official",
                         help="official=官方 Claude Code 映射；pro=Opus 使用 deepseek-v4-pro")
    install.add_argument("--main-model")
    install.add_argument("--opus-model")
    install.add_argument("--sonnet-model")
    install.add_argument("--haiku-model")
    install.add_argument("--subagent-model")
    install.add_argument("--effort", choices=["low", "medium", "high", "max"], default="max")
    install.add_argument("--skip-service", action="store_true", help="只配置终端 CLI，不重启 CloudCLI")
    install.add_argument("--no-restart", action="store_true", help="写入配置但不重启服务")
    install.add_argument("--no-health", action="store_true", help="不检查 CloudCLI /health")
    install.add_argument("--dry-run", action="store_true", help="只显示计划，不写文件")
    install.add_argument("--yes", action="store_true")
    install.add_argument("--force", action="store_true", help="允许覆盖非本工具创建的冲突文件")

    status = sub.add_parser("status", help="显示配置和服务状态（不输出 key）")
    status.add_argument("--user")
    status.add_argument("--json", action="store_true")

    restore = sub.add_parser("restore", help="按安装时的备份还原，并移除本工具文件")
    restore.add_argument("--user")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--force", action="store_true", help="允许覆盖安装后被修改的文件")

    probe_api_parser = sub.add_parser("probe-api", help="直接向 DeepSeek Messages API 发一次计费探针")
    probe_api_parser.add_argument("--user")
    probe_api_parser.add_argument("--model")
    probe_api_parser.add_argument("--max-tokens", type=int, default=64)
    probe_api_parser.add_argument("--prompt", default="只回复 OK，不要解释。")
    probe_api_parser.add_argument("--yes", action="store_true")

    probe_cli_parser = sub.add_parser("probe-cli", help="通过 claude-deepseek 发一次计费探针")
    probe_cli_parser.add_argument("--user")
    probe_cli_parser.add_argument("--prompt", default="只回复 OK，不要使用任何工具。")
    probe_cli_parser.add_argument("--yes", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            if args.skip_service and not UNIT_PATH.exists() and not args.user:
                raise ToolError(
                    "未找到 CloudCLI 服务单元，且没有指定 --user；"
                    "请用 --user <工作用户> 指定终端配置的目标用户。"
                )
            paths = resolve_paths(
                args.user,
                require_service=not args.skip_service,
                require_root=True,
            )
            models = resolve_models(args)
            if args.dry_run:
                key = "dry-run-placeholder-key-0000"
                if any((args.key_file, args.env_file, args.key_stdin)):
                    key = read_secret(args)
                print_plan(paths, models, args)
                result = apply_config(
                    paths, key, models, profile=args.profile, force=args.force,
                    restart=not args.no_restart, health=not args.no_health,
                    dry_run=True,
                )
                print("dry-run 结果: %s" % json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            key = read_secret(args)
            print_plan(paths, models, args)
            confirm("确认写入以上配置？", args.yes)
            result = apply_config(
                paths, key, models, profile=args.profile, force=args.force,
                restart=not args.no_restart, health=not args.no_health,
            )
            state = result["state"]
            print("已安装。环境文件 %s；备份 %s"
                  % (paths.env, (state.get("backup") or {}).get("backupDir")))
            print("服务重启: %s" % ("是" if result["restarted"] else "否"))
            print("终端使用: %s" % paths.wrapper)
            print("CloudCLI 网页: 打开后 Claude 账户页应显示 API key/Auth Token，而不是 OAuth 登录。")
            return 0

        if args.command == "status":
            paths = resolve_paths(
                args.user,
                require_service=False,
                require_root=False,
            )
            status = collect_status(paths)
            print_status(status, args.json)
            return 0

        if args.command == "restore":
            initial_state = load_state(STATE_ROOT / "state.json") if (STATE_ROOT / "state.json").exists() else None
            user = args.user or (initial_state.get("targetUser") if initial_state else None)
            if not user:
                raise ToolError("没有安装状态；请用 --user 指定要检查/还原的用户。")
            paths = resolve_paths(
                user,
                require_service=False,
                require_root=True,
            )
            confirm("确认按备份还原并移除本工具写入的配置？", args.yes)
            result = restore_config(paths, force=args.force)
            print("已还原。备份保留在 %s" % result.get("backupDir"))
            return 0

        if args.command in ("probe-api", "probe-cli"):
            paths = resolve_paths(
                args.user,
                require_service=False,
                require_root=False,
            )
            if args.command == "probe-api":
                probe_api(paths, args)
            else:
                probe_cli(paths, args)
            return 0

        parser.error("未知命令")
        return 2
    except ToolError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1
    except OSError as exc:
        print("错误: 文件或系统调用失败：%s" % (exc.strerror or exc), file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1
    except KeyboardInterrupt:
        print("已中断，未完成的操作请用 status 检查。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
