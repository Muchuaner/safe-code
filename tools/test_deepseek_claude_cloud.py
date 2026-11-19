#!/usr/bin/env python3
"""Unit tests for the isolated DeepSeek/CloudCLI configuration tool.

These tests never contact DeepSeek, CloudCLI, systemd, or any VM.  They use
temporary directories and a fake systemctl to exercise backup, install,
idempotency, restore, redaction, and template behavior.
"""

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().with_name("deepseek_claude_cloud.py")
SPEC = importlib.util.spec_from_file_location("deepseek_claude_cloud", MODULE_PATH)
assert SPEC and SPEC.loader
D = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = D
SPEC.loader.exec_module(D)

KEY1 = "sk-" + "a" * 32
KEY2 = "sk-" + "b" * 32


class FakeSystemctl:
    def __init__(self, env_path, active=True, listed=True):
        self.env_path = Path(env_path)
        self.active = active
        self.listed = listed
        self.calls = []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n" if self.active else "inactive\n", "")
        if args[0] == "show" and "EnvironmentFiles" in args:
            value = "%s (ignore_errors=no)\n" % self.env_path if self.listed else "\n"
            return subprocess.CompletedProcess(args, 0, value, "")
        if args[0] == "show" and "Environment" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def restart_count(self):
        return sum(1 for call in self.calls if call[0] == "restart")


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        self.etc = root / "etc" / "systemd" / "system"
        self.etc.mkdir(parents=True)
        self.unit = self.etc / "safe-code-cloudcli.service"
        self.unit.write_text("[Service]\nUser=testuser\n", "utf-8")
        self.dropin = self.etc / "safe-code-cloudcli.service.d" / "50-deepseek.conf"
        self.state = root / "var" / "lib" / "safe-code-deepseek" / "state.json"
        self.backup = root / "var" / "backups" / "safe-code-deepseek"
        self.paths = D.Paths(
            user="testuser",
            uid=os.getuid(),
            gid=os.getgid(),
            home=self.home,
            env=self.home / ".config" / "safe-code" / "deepseek-claude.env",
            wrapper=self.home / ".local" / "bin" / "claude-deepseek",
            dropin=self.dropin,
            unit=self.unit,
            state=self.state,
            backup_root=self.backup,
        )

    def systemctl(self, **kwargs):
        return FakeSystemctl(self.paths.env, **kwargs)


def official_models():
    return {
        "main": "deepseek-flash[1m]",
        "opus": "deepseek-flash[1m]",
        "sonnet": "deepseek-flash[1m]",
        "haiku": "deepseek-flash",
        "subagent": "deepseek-flash",
        "effort": "max",
    }


class RenderingTests(unittest.TestCase):
    def test_official_mapping_matches_deepseek_claude_code_guide(self):
        models = official_models()
        text = D.render_env(KEY1, models).decode("utf-8")
        self.assertIn("ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic", text)
        self.assertIn("ANTHROPIC_MODEL=deepseek-flash[1m]", text)
        self.assertIn("ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-flash[1m]", text)
        self.assertIn("ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-flash[1m]", text)
        self.assertIn("ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-flash", text)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=deepseek-flash", text)
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL=max", text)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=786432", text)
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", text)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", text)
        self.assertNotIn("ANTHROPIC_API_KEY", text)

    def test_pro_profile_only_changes_opus(self):
        models = D.resolve_models(type("Args", (), {
            "profile": "pro", "main_model": None, "opus_model": None, "sonnet_model": None,
            "haiku_model": None, "subagent_model": None, "effort": "max",
        })())
        self.assertEqual(models["opus"], "deepseek-v4-pro")
        self.assertEqual(models["sonnet"], "deepseek-flash[1m]")
        self.assertEqual(models["haiku"], "deepseek-flash")

    def test_wrapper_does_not_source_env_file(self):
        text = D.render_wrapper().decode("utf-8")
        self.assertIn("safe-code-deepseek-wrapper-v1", text)
        self.assertIn("while IFS='=' read -r key value", text)
        self.assertIn("ANTHROPIC_*|CLAUDE_CODE_*|DISABLE_*", text)
        self.assertIn('exec "$CLAUDE_BIN" "$@"', text)
        self.assertNotIn("source ", text)
        self.assertNotIn(". \"$ENV_FILE\"", text)
        self.assertNotIn("eval ", text)

    def test_wrapper_is_valid_posix_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "claude-deepseek"
            path.write_bytes(D.render_wrapper())
            path.chmod(0o755)
            result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_enforces_0600_and_allowlisted_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env_file = root / "deepseek.env"
            fake_claude = root / "claude"
            wrapper = root / "claude-deepseek"
            token = "sk-" + "c" * 32
            env_file.write_text(
                "ANTHROPIC_AUTH_TOKEN=%s\nCLAUDE_CODE_EFFORT_LEVEL=max\nOTHER=must-not-export\n" % token,
                "utf-8",
            )
            env_file.chmod(0o644)
            fake_claude.write_text(
                "#!/bin/sh\nprintf '%s|%s|%s\\n' \"$ANTHROPIC_AUTH_TOKEN\" \"$CLAUDE_CODE_EFFORT_LEVEL\" \"${OTHER:-unset}\"\n",
                "utf-8",
            )
            fake_claude.chmod(0o755)
            wrapper.write_bytes(D.render_wrapper())
            wrapper.chmod(0o755)
            base_env = {
                "HOME": str(root),
                "SAFE_CODE_DEEPSEEK_ENV": str(env_file),
                "SAFE_CODE_CLAUDE_BIN": str(fake_claude),
                "PATH": "/usr/bin:/bin",
            }
            refused = subprocess.run(["sh", str(wrapper)], capture_output=True, text=True, env=base_env)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("600", refused.stderr)
            env_file.chmod(0o600)
            allowed = subprocess.run(["sh", str(wrapper)], capture_output=True, text=True, env=base_env)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(allowed.stdout.strip(), "%s|max|unset" % token)

    def test_dropin_uses_environment_file_without_inline_secret(self):
        text = D.render_dropin(Path("/home/workspace/.config/safe-code/deepseek-claude.env")).decode("utf-8")
        self.assertIn("EnvironmentFile=/home/workspace/.config/safe-code/deepseek-claude.env", text)
        self.assertIn("UnsetEnvironment=ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN", text)
        self.assertIn("LimitCORE=0", text)
        self.assertNotIn("sk-", text)


class ValidationTests(unittest.TestCase):
    def test_key_validation(self):
        self.assertEqual(D.validate_key(KEY1), KEY1)
        for bad in ("", "short", "sk-" + "a" * 10, "sk-" + "a" * 32 + "!", "sk-" + "a" * 20 + " b"):
            with self.assertRaises(D.ToolError):
                D.validate_key(bad)
        with self.assertRaises(D.ToolError):
            D.validate_key("your-api-key-here-000000")

    def test_env_parser(self):
        values = D.parse_env_text("# comment\nDEEPSEEK_API_KEY=%s\n" % KEY1)
        self.assertEqual(values["DEEPSEEK_API_KEY"], KEY1)
        with self.assertRaises(D.ToolError):
            D.parse_env_text("NO_EQUALS\n")
        with self.assertRaises(D.ToolError):
            D.parse_env_text("A=1\nA=2\n")

    def test_model_validation(self):
        for good in ("deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"):
            self.assertEqual(D.validate_model(good, "test"), good)
        for bad in ("claude-sonnet-4-5", "deepseek-flash[2m]", "", "deepseek-v4-pro[1m]"):
            with self.assertRaises(D.ToolError):
                D.validate_model(bad, "test")

    def test_key_file_requires_0600(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "key"
            path.write_text(KEY1 + "\n", "utf-8")
            path.chmod(0o644)
            with self.assertRaises(D.ToolError):
                D.read_key_file(path, "key", [os.getuid()])
            path.chmod(0o600)
            self.assertEqual(D.read_key_file(path, "key", [os.getuid()]), KEY1)

    def test_env_input_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.env"
            path.write_text("ANTHROPIC_AUTH_TOKEN=%s\nOTHER=1\n" % KEY1, "utf-8")
            path.chmod(0o600)
            with self.assertRaises(D.ToolError):
                D.read_key_file(path, "env", [os.getuid()])

    def test_parse_unit_user(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unit"
            path.write_text("[Service]\nUser = workspace\n", "utf-8")
            self.assertEqual(D.parse_unit_user(path), "workspace")
            path.write_text("[Service]\n", "utf-8")
            self.assertIsNone(D.parse_unit_user(path))


class ProbeTests(unittest.TestCase):
    def test_probe_api_uses_official_endpoint_and_redacts_key(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            h.paths.env.parent.mkdir(parents=True)
            h.paths.env.write_text(D.render_env(KEY1, official_models()).decode("utf-8"), "utf-8")
            h.paths.env.chmod(0o600)
            seen = {}

            class Response:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self, _limit=-1):
                    return json.dumps({
                        "model": "deepseek-flash",
                        "content": [{"type": "text", "text": "OK"}],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    }).encode("utf-8")

            def fake_urlopen(request, timeout=0):
                seen["url"] = request.full_url
                seen["auth"] = request.get_header("Authorization")
                return Response()

            args = types.SimpleNamespace(yes=True, model=None, max_tokens=32, prompt="只回复 OK")
            output = io.StringIO()
            with mock.patch.object(D.urllib.request, "urlopen", fake_urlopen):
                with contextlib.redirect_stdout(output):
                    D.probe_api(h.paths, args)
            text = output.getvalue()
            self.assertEqual(seen["url"], D.OFFICIAL_BASE_URL + "/v1/messages")
            self.assertIn("Bearer " + KEY1, seen["auth"])
            self.assertIn("OK", text)
            self.assertNotIn(KEY1, text)

    def test_probe_api_refuses_wide_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            h.paths.env.parent.mkdir(parents=True)
            h.paths.env.write_text(D.render_env(KEY1, official_models()).decode("utf-8"), "utf-8")
            h.paths.env.chmod(0o644)
            args = types.SimpleNamespace(yes=True, model=None, max_tokens=32, prompt="hi")
            with self.assertRaises(D.ToolError):
                D.probe_api(h.paths, args)


class HealthRetryTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.now = 0.0
        def __call__(self):
            return self.now
        def sleep(self, seconds):
            self.now += seconds

    def test_health_retries_transient_refusal_and_fails_within_budget(self):
        # First connection is refused, second succeeds: no error, two attempts.
        clock = self.Clock()
        calls = []
        def flaky(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise ConnectionRefusedError("connection refused")
        D.wait_for_health(
            total_timeout=2.0, interval=0.5, health_fn=flaky,
            clock_fn=clock, sleep_fn=clock.sleep,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], 2.0)
        self.assertTrue(all(0 < value <= 2.0 for value in calls))

        # Permanently refused: bounded attempts and last error is reported.
        clock2 = self.Clock()
        calls2 = []
        def down(timeout):
            calls2.append(timeout)
            raise ConnectionRefusedError("still refused")
        with self.assertRaises(D.ToolError) as raised:
            D.wait_for_health(
                total_timeout=2.0, interval=0.5, health_fn=down,
                clock_fn=clock2, sleep_fn=clock2.sleep,
            )
        self.assertEqual(len(calls2), 4)
        message = str(raised.exception)
        self.assertIn("未就绪", message)
        self.assertIn("still refused", message)
        self.assertIn("尝试 4 次", message)


class InstallTests(unittest.TestCase):
    def test_install_status_and_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            fake = h.systemctl()
            result = D.apply_config(h.paths, KEY1, official_models(), profile="official",
                                    health=False, systemctl_fn=fake)
            self.assertTrue(Path(h.paths.env).is_file())
            self.assertEqual(stat.S_IMODE(Path(h.paths.env).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(h.paths.wrapper).stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(Path(h.paths.dropin).stat().st_mode), 0o644)
            self.assertIn(KEY1, Path(h.paths.env).read_text("utf-8"))
            self.assertEqual(result["state"]["status"], "installed")
            self.assertTrue(result["restarted"])
            self.assertEqual(fake.restart_count(), 1)

            status = D.collect_status(h.paths, fake)
            dumped = json.dumps(status, ensure_ascii=False)
            self.assertNotIn(KEY1, dumped)
            self.assertTrue(status["keySet"])
            self.assertEqual(status["envMode"], "600")
            self.assertTrue(status["environmentFileListed"])
            self.assertFalse(status["oauthCredentialsPresent"])

    def test_install_is_idempotent_and_keeps_one_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            fake = h.systemctl()
            D.apply_config(h.paths, KEY1, official_models(), profile="official",
                           health=False, systemctl_fn=fake)
            D.apply_config(h.paths, KEY1, official_models(), profile="official",
                           health=False, systemctl_fn=fake)
            backups = [p for p in h.backup.iterdir() if p.is_dir()]
            self.assertEqual(len(backups), 1)
            self.assertEqual(fake.restart_count(), 1)  # identical reinstall does not restart
            D.apply_config(h.paths, KEY2, official_models(), profile="official",
                           health=False, systemctl_fn=fake)
            self.assertIn(KEY2, Path(h.paths.env).read_text("utf-8"))
            self.assertEqual(fake.restart_count(), 2)  # changed key does restart
            self.assertEqual(len([p for p in h.backup.iterdir() if p.is_dir()]), 1)
            self.assertEqual(D.load_state(h.paths.state)["status"], "installed")

    def test_backup_restore_original_env(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            h.paths.env.parent.mkdir(parents=True)
            old = "ANTHROPIC_AUTH_TOKEN=%s\nANTHROPIC_BASE_URL=%s\n" % (KEY2, D.OFFICIAL_BASE_URL)
            h.paths.env.write_text(old, "utf-8")
            h.paths.env.chmod(0o600)
            fake = h.systemctl()
            D.apply_config(h.paths, KEY1, official_models(), profile="official",
                           health=False, systemctl_fn=fake)
            state = D.load_state(h.paths.state)
            backup_dir = Path(state["backup"]["backupDir"])
            backup_env = (backup_dir / "env").read_text("utf-8")
            self.assertIn(KEY2, backup_env)
            manifest = (backup_dir / "manifest.json").read_text("utf-8")
            self.assertNotIn(KEY1, manifest)
            self.assertNotIn(KEY2, manifest)

            D.restore_config(h.paths, systemctl_fn=fake)
            self.assertEqual(h.paths.env.read_text("utf-8"), old)
            self.assertFalse(h.paths.wrapper.exists())
            self.assertFalse(h.paths.dropin.exists())
            self.assertEqual(D.load_state(h.paths.state)["status"], "restored")

    def test_restore_refuses_user_modification_unless_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            fake = h.systemctl()
            D.apply_config(h.paths, KEY1, official_models(), profile="official",
                           health=False, systemctl_fn=fake)
            h.paths.wrapper.write_text("#!/bin/sh\necho user change\n", "utf-8")
            with self.assertRaises(D.ToolError):
                D.restore_config(h.paths, systemctl_fn=fake)
            D.restore_config(h.paths, force=True, systemctl_fn=fake)
            self.assertFalse(h.paths.wrapper.exists())

    def test_restore_recovers_from_interrupted_install_state(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            h.paths.env.parent.mkdir(parents=True)
            old = "ANTHROPIC_AUTH_TOKEN=%s\n" % KEY2
            h.paths.env.write_text(old, "utf-8")
            h.paths.env.chmod(0o600)
            roles = {"env": h.paths.env, "wrapper": h.paths.wrapper, "dropin": h.paths.dropin}
            backup = D.create_backup(h.paths, roles)
            state = {
                "version": 1,
                "status": "installing",
                "targetUser": "testuser",
                "backup": backup,
                "files": {
                    role: {
                        "path": str(path),
                        "existed": backup["files"][role]["existed"],
                        "backup": backup["files"][role]["backup"],
                        "originalSha256": backup["files"][role]["sha256"],
                        "installedSha256": None,
                        "mode": backup["files"][role]["mode"],
                        "uid": backup["files"][role]["uid"],
                        "gid": backup["files"][role]["gid"],
                    }
                    for role, path in roles.items()
                },
                "service": {"activeBefore": False},
            }
            D.save_state(h.paths.state, state)
            h.paths.env.write_text("partial-new-content\n", "utf-8")
            h.paths.env.chmod(0o600)
            fake = h.systemctl(active=False)
            D.restore_config(h.paths, systemctl_fn=fake)
            self.assertEqual(h.paths.env.read_text("utf-8"), old)
            self.assertEqual(D.load_state(h.paths.state)["status"], "restored")

    def test_install_rejects_unmanaged_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            h.paths.env.parent.mkdir(parents=True)
            h.paths.env.write_text("OTHER_SETTING=1\n", "utf-8")
            h.paths.env.chmod(0o600)
            fake = h.systemctl()
            with self.assertRaises(D.ToolError):
                D.apply_config(h.paths, KEY1, official_models(), profile="official",
                               health=False, systemctl_fn=fake)
            self.assertEqual(h.paths.env.read_text("utf-8"), "OTHER_SETTING=1\n")
            self.assertFalse(h.paths.dropin.exists())

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            h = Harness(Path(folder))
            fake = h.systemctl()
            result = D.apply_config(
                h.paths, "dry-run-placeholder-key-0000", official_models(),
                profile="official", health=False, dry_run=True, systemctl_fn=fake,
            )
            self.assertTrue(result["dryRun"])
            self.assertFalse(h.paths.env.exists())
            self.assertFalse(h.paths.dropin.exists())
            self.assertFalse(h.paths.state.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
