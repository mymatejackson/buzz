"""Offline protocol and launchd tests; no SSH connection or live identity."""
import base64
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = Path(__file__).parent
SOURCE = HERE / "buzz-backend-ssh"
FIXTURE = json.loads((HERE / "fixtures" / "nsec-scalar-one.json").read_text())
loader = importlib.machinery.SourceFileLoader("provider", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
provider = importlib.util.module_from_spec(spec)
loader.exec_module(provider)


def helper():
    module = types.ModuleType("remote_helper")
    module.__dict__["_SOURCE"] = provider.REMOTE.encode()
    exec(compile(provider.REMOTE, "<remote>", "exec"), module.__dict__)
    return module


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.running = False
        self.calls = []
        self.args = []
        self.plist_path = None
        self.fail_bootstrap_count = 0
        self.exit_next_bootstrap = False
        self.pid = 4242

    def __call__(self, *args):
        self.calls.append(args)
        domain = "gui/" + str(os.getuid())
        if args == ("print", domain):
            return subprocess.CompletedProcess(args, 0, "domain", "")
        if args[0] == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "")
            lines = [
                "state = " + ("running" if self.running else "exited"),
                "path = " + str(self.plist_path),
                "arguments = {",
                *["    " + value for value in self.args],
                "}",
            ]
            if self.running:
                lines.append("pid = " + str(self.pid))
            return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n", "")
        if args[0] == "bootout":
            self.loaded = False
            self.running = False
            self.args = []
            self.plist_path = None
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "bootstrap":
            if self.fail_bootstrap_count:
                self.fail_bootstrap_count -= 1
                return subprocess.CompletedProcess(args, 1, "", "")
            value = plistlib.loads(Path(args[2]).read_bytes())
            self.loaded = True
            self.args = value["ProgramArguments"]
            self.plist_path = Path(args[2])
            self.running = bool(value.get("RunAtLoad")) and not self.exit_next_bootstrap
            self.exit_next_bootstrap = False
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.remote = helper()
        self.remote.START_STABILITY_SECONDS = 0.001
        self.remote.START_TIMEOUT_SECONDS = 1.0
        self.secret = FIXTURE["nsec"]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / ".buzz-ssh-provider"
        self.fake = FakeLaunchctl()
        self.remote.launchctl = self.fake
        self.remote.shutil.which = (
            lambda name, path=None: "/usr/bin/true"
            if name in ("buzz-acp", "codex-acp") else None
        )
        self.request = {
            "op": "deploy",
            "provider_config": {
                "host": "example",
                "remote_directory": str(self.root),
            },
            "agent": {
                "relay_url": "wss://relay.example",
                "private_key_nsec": self.secret,
                "auth_tag": "fake-auth-tag",
                "provider": "openai",
                "launch": {
                    "command": "codex-acp",
                    "args": [],
                    "owner_pubkey": "a" * 64,
                    "env": {"MODEL": "test"},
                    "policy_env": {"BUZZ_ACP_AGENTS": "1"},
                },
            },
        }

    def test_info_and_single_file_staging(self):
        staged = Path(self.temp.name) / "provider"
        shutil.copy2(SOURCE, staged)
        result = subprocess.run(
            [str(staged)], input=b'{"op":"info"}', capture_output=True
        )
        self.assertEqual(result.returncode, 0)
        info = json.loads(result.stdout)
        self.assertEqual(info["protocol_version"], 1)
        self.assertEqual(
            set(info["config_schema"]["required"]), {"host", "remote_directory"}
        )
        self.assertEqual(
            set(info["config_schema"]["properties"]),
            {"host", "remote_directory", "runtime_command"},
        )

    def test_rust_nostr_scalar_one_fixture(self):
        self.assertIn("nostr::SecretKey", FIXTURE["source"])
        self.assertEqual(self.remote.decode_nsec(FIXTURE["nsec"]), 1)
        self.assertEqual(self.remote.pubkey(1), FIXTURE["pubkey_hex"])
        mutated = FIXTURE["nsec"][:-1] + ("q" if FIXTURE["nsec"][-1] != "q" else "p")
        with self.assertRaisesRegex(ValueError, "invalid agent identity"):
            self.remote.decode_nsec(mutated)

    def test_identity_and_owner_fail_before_writes(self):
        for field, value in (("private_key_nsec", "not-a-key"), ("relay_url", "")):
            req = json.loads(json.dumps(self.request))
            req["agent"][field] = value
            with self.assertRaises(ValueError):
                self.remote.deploy(req)
            self.assertFalse(self.root.exists())
        req = json.loads(json.dumps(self.request))
        req["agent"]["launch"]["owner_pubkey"] = ""
        with self.assertRaises(ValueError):
            self.remote.deploy(req)
        self.assertFalse(self.root.exists())

    def test_ssh_argv_no_secret_and_host_injection_refused(self):
        with mock.patch.object(provider.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"")
            provider.respond(self.request)
            argv = run.call_args.args[0]
            wire = run.call_args.kwargs["input"]
            self.assertIn(self.secret.encode(), wire)
            self.assertNotIn(self.secret, " ".join(argv))
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("BatchMode=yes", argv)
        bad = json.loads(json.dumps(self.request))
        bad["provider_config"]["host"] = "-oProxyCommand=evil"
        with mock.patch.object(provider.subprocess, "run") as run:
            self.assertFalse(provider.respond(bad)["ok"])
            run.assert_not_called()

    def test_idempotent_and_changed_running_refusal(self):
        first = self.remote.deploy(self.request)
        self.assertEqual(self.remote.deploy(self.request), first)
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 1)
        digest = first.split(".")[-1]
        state = self.root / "state" / (digest + ".json")
        plist_path = self.root / "jobs" / (first + ".plist")
        plist = plistlib.loads(plist_path.read_bytes())
        helper_path = Path(plist["ProgramArguments"][1])
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertRegex(helper_path.name, r"helper-[0-9a-f]{20}-[0-9a-f]{16}\.py")
        self.assertEqual(helper_path.parent, self.root / "helpers")
        self.assertFalse((self.root / "helper.py").exists())
        self.assertFalse(plist["KeepAlive"])
        self.assertNotIn(self.secret, str(plist))
        changed = json.loads(json.dumps(self.request))
        changed["agent"]["launch"]["env"]["MODEL"] = "different"
        with self.assertRaisesRegex(ValueError, "stop it before redeploy"):
            self.remote.deploy(changed)
        self.assertIn(self.secret, state.read_text())

    def test_stopped_job_restarts(self):
        label = self.remote.deploy(self.request)
        self.fake.running = False
        self.assertEqual(self.remote.deploy(self.request), label)
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 2)

    def test_stage_failure_before_bootout_leaves_old_job_and_files(self):
        label = self.remote.deploy(self.request)
        self.fake.running = False
        digest = label.split(".")[-1]
        state = self.root / "state" / (digest + ".json")
        plist = self.root / "jobs" / (label + ".plist")
        before_state = state.read_bytes()
        before_plist = plist.read_bytes()
        changed = json.loads(json.dumps(self.request))
        changed["agent"]["launch"]["env"]["MODEL"] = "changed"
        real_stage = self.remote.stage_file

        def fail_plist(path, data, mode):
            if path == plist:
                raise OSError("injected plist stage failure")
            return real_stage(path, data, mode)

        marker = len(self.fake.calls)
        with mock.patch.object(self.remote, "stage_file", side_effect=fail_plist):
            with self.assertRaises(OSError):
                self.remote.deploy(changed)
        self.assertEqual(state.read_bytes(), before_state)
        self.assertEqual(plist.read_bytes(), before_plist)
        self.assertTrue(self.fake.loaded)
        self.assertFalse(self.fake.running)
        self.assertFalse(any(call[0] == "bootout" for call in self.fake.calls[marker:]))

    def test_commit_failure_restores_stopped_job_and_old_files(self):
        label = self.remote.deploy(self.request)
        self.fake.running = False
        digest = label.split(".")[-1]
        state = self.root / "state" / (digest + ".json")
        plist = self.root / "jobs" / (label + ".plist")
        before_state = state.read_bytes()
        before_plist = plist.read_bytes()
        old_helper = Path(plistlib.loads(before_plist)["ProgramArguments"][1])
        self.remote._SOURCE += b"\n"
        changed = json.loads(json.dumps(self.request))
        changed["agent"]["launch"]["env"]["MODEL"] = "changed"
        real_install = self.remote.install_staged
        failed = False

        def fail_once(staged, destination):
            nonlocal failed
            if destination == plist and not failed:
                failed = True
                raise OSError("injected commit failure")
            return real_install(staged, destination)

        with mock.patch.object(self.remote, "install_staged", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.remote.deploy(changed)
        self.assertEqual(state.read_bytes(), before_state)
        self.assertEqual(plist.read_bytes(), before_plist)
        self.assertTrue(self.fake.loaded)
        self.assertFalse(self.fake.running)
        self.assertTrue(old_helper.is_file())
        self.assertEqual(list((self.root / "helpers").iterdir()), [old_helper])

    def test_historical_helper_record_is_owned(self):
        label = self.remote.deploy(self.request)
        self.fake.running = False
        digest = label.split(".")[-1]
        plist_path = self.root / "jobs" / (label + ".plist")
        value = plistlib.loads(plist_path.read_bytes())
        historical = self.root / "helper.py"
        historical.write_bytes(provider.REMOTE.encode())
        historical.chmod(0o600)
        value["ProgramArguments"][1] = str(historical)
        plist_path.write_bytes(plistlib.dumps(value))
        plist_path.chmod(0o600)
        self.fake.args = value["ProgramArguments"]
        self.assertEqual(self.remote.deploy(self.request), label)

    def test_other_root_loaded_job_is_never_adopted_or_unloaded(self):
        label = self.remote.deploy(self.request)
        digest = label.split(".")[-1]
        other_parent = Path(self.temp.name).resolve() / "other-project"
        other_parent.mkdir(mode=0o700)
        other_root = other_parent / ".buzz-ssh-provider"
        for directory in (other_root, other_root / "state", other_root / "jobs"):
            directory.mkdir(mode=0o700)
        source_state = self.root / "state" / (digest + ".json")
        other_state = other_root / "state" / (digest + ".json")
        other_state.write_bytes(source_state.read_bytes())
        other_state.chmod(0o600)
        other_plist = other_root / "jobs" / (label + ".plist")
        local_looking = {
            "Label": label,
            "ProgramArguments": [
                sys.executable, str(other_root / "helper.py"),
                "run", str(other_root), digest,
            ],
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        other_plist.write_bytes(plistlib.dumps(local_looking))
        other_plist.chmod(0o600)
        request = json.loads(json.dumps(self.request))
        request["provider_config"]["remote_directory"] = str(other_root)
        marker = len(self.fake.calls)
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.remote.deploy(request)
        self.assertFalse(any(call[0] == "bootout" for call in self.fake.calls[marker:]))

    def test_isolated_profiles_override_launch_env_and_run_does_not_inherit(self):
        for name in (
            "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "BUZZ_AGENT_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
            "TMPDIR", "PATH",
        ):
            self.request["agent"]["launch"]["env"][name] = "/tmp/should-not-win"
        label = self.remote.deploy(self.request)
        digest = label.split(".")[-1]
        state = json.loads(
            (self.root / "state" / (digest + ".json")).read_text()
        )
        env = state["env"]
        work = (self.root / "work" / digest).resolve()
        for name in (
            "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "BUZZ_AGENT_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "TMPDIR",
        ):
            self.assertTrue(Path(env[name]).resolve().is_relative_to(work))
        self.assertNotEqual(env["PATH"], "/tmp/should-not-win")
        with mock.patch.dict(os.environ, {"PARENT_ONLY_TEST": "must-not-leak"}):
            with mock.patch.object(self.remote.os, "dup2"), \
                 mock.patch.object(self.remote.os, "chdir"), \
                 mock.patch.object(
                     self.remote.os, "execve", side_effect=RuntimeError("captured")
                 ) as execute:
                with self.assertRaisesRegex(RuntimeError, "captured"):
                    self.remote.run(str(self.root), digest)
        executed_env = execute.call_args.args[2]
        self.assertNotIn("PARENT_ONLY_TEST", executed_env)
        self.assertEqual(executed_env, env)
        Path(env["HOME"]).chmod(0o755)
        with self.assertRaisesRegex(ValueError, "managed environment is invalid"):
            self.remote.run(str(self.root), digest)

    def test_missing_env_shebang_interpreter_refuses_before_writes(self):
        script = Path(self.temp.name) / "runtime"
        script.write_text("#!/usr/bin/env definitely-not-a-buzz-test-tool\n")
        script.chmod(0o700)
        self.request["provider_config"]["runtime_command"] = str(script)
        with self.assertRaisesRegex(ValueError, "interpreter is unavailable"):
            self.remote.deploy(self.request)
        self.assertFalse(self.root.exists())

    def test_immediate_exit_is_failure_and_cleans_new_ownership_files(self):
        self.fake.exit_next_bootstrap = True
        with self.assertRaisesRegex(ValueError, "exited during startup"):
            self.remote.deploy(self.request)
        self.assertFalse(self.fake.loaded)
        self.assertEqual(list((self.root / "state").iterdir()), [])
        self.assertEqual(list((self.root / "jobs").iterdir()), [])
        self.assertEqual(list((self.root / "helpers").iterdir()), [])

    def test_remote_command_cannot_be_client_path(self):
        req = json.loads(json.dumps(self.request))
        req["agent"]["launch"]["command"] = (
            "/Applications/Some.app/Contents/MacOS/codex-acp"
        )
        with self.assertRaisesRegex(ValueError, "remote PATH"):
            self.remote.deploy(req)
        self.assertFalse(self.root.exists())

    def test_concurrent_identical_deploy_has_one_bootstrap(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            labels = list(pool.map(self.remote.deploy, [self.request, self.request]))
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 1)

    def test_concurrent_different_agents_failed_deploy_keeps_success_helper(self):
        request_ok = json.loads(json.dumps(self.request))
        request_fail = json.loads(json.dumps(self.request))
        request_fail["agent"]["relay_url"] = "wss://other-relay.example"
        identity = self.remote.pubkey(self.remote.decode_nsec(self.secret))
        digest_fail = self.remote.hashlib.sha256(
            (request_fail["agent"]["relay_url"] + "\0" + identity).encode()
        ).hexdigest()[:20]
        fail_label = "local.buzz.ssh." + digest_fail
        domain = "gui/" + str(os.getuid())
        barrier = threading.Barrier(2)
        success_seen = threading.Event()
        jobs = {}
        jobs_lock = threading.Lock()

        def launchctl(*args):
            if args == ("print", domain):
                return subprocess.CompletedProcess(args, 0, "domain", "")
            if args[0] == "print":
                label = args[1].split("/")[-1]
                with jobs_lock:
                    job = jobs.get(label)
                if job is None:
                    return subprocess.CompletedProcess(args, 113, "", "")
                success_seen.set()
                lines = [
                    "state = running",
                    "path = " + job["path"],
                    "arguments = {",
                    *["    " + value for value in job["args"]],
                    "}",
                    "pid = 5252",
                ]
                return subprocess.CompletedProcess(
                    args, 0, "\n".join(lines) + "\n", ""
                )
            if args[0] == "bootstrap":
                value = plistlib.loads(Path(args[2]).read_bytes())
                label = value["Label"]
                barrier.wait(timeout=2)
                if label == fail_label:
                    if not success_seen.wait(timeout=2):
                        raise AssertionError("successful job was not observed")
                    return subprocess.CompletedProcess(args, 1, "", "")
                with jobs_lock:
                    jobs[label] = {
                        "path": str(Path(args[2])),
                        "args": value["ProgramArguments"],
                    }
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "bootout":
                with jobs_lock:
                    jobs.pop(args[1].split("/")[-1], None)
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        self.remote.launchctl = launchctl
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            success_future = pool.submit(self.remote.deploy, request_ok)
            failed_future = pool.submit(self.remote.deploy, request_fail)
            success_label = success_future.result()
            with self.assertRaisesRegex(ValueError, "bootstrap"):
                failed_future.result()
        success_digest = success_label.split(".")[-1]
        success_plist = plistlib.loads(
            (self.root / "jobs" / (success_label + ".plist")).read_bytes()
        )
        success_helper = Path(success_plist["ProgramArguments"][1])
        self.assertTrue(success_helper.is_file())
        self.assertIn(success_digest, success_helper.name)
        self.assertEqual(list((self.root / "helpers").iterdir()), [success_helper])
        self.assertFalse((self.root / "state" / (digest_fail + ".json")).exists())
        self.assertFalse((self.root / "jobs" / (fail_label + ".plist")).exists())

    def test_remote_error_cannot_echo_a_secret(self):
        response = json.dumps({"ok": False, "error": self.secret}).encode()
        with mock.patch.object(provider.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, response, b"")
            result = provider.respond(self.request)
        self.assertEqual(
            result, {"ok": False, "error": "remote deployment refused"}
        )

    def test_missing_remote_runtime_refuses_before_writes(self):
        self.remote.shutil.which = lambda name, path=None: None
        with self.assertRaisesRegex(ValueError, "runtime_command is unavailable"):
            self.remote.deploy(self.request)
        self.assertFalse(self.root.exists())

    def test_managed_directory_symlink_is_refused(self):
        elsewhere = Path(self.temp.name) / "other"
        elsewhere.mkdir()
        self.root.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.remote.deploy(self.request)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unowned_stopped_label_is_never_unloaded(self):
        self.fake.loaded = True
        self.fake.running = False
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.remote.deploy(self.request)
        self.assertFalse(any(call[0] == "bootout" for call in self.fake.calls))
        self.assertEqual(list((self.root / "state").iterdir()), [])

    def test_failed_new_bootstrap_cleans_files_without_bootout(self):
        self.fake.fail_bootstrap_count = 1
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            self.remote.deploy(self.request)
        self.assertFalse(any(call[0] == "bootout" for call in self.fake.calls))
        self.assertEqual(list((self.root / "state").iterdir()), [])
        self.assertEqual(list((self.root / "jobs").iterdir()), [])
        self.assertEqual(list((self.root / "helpers").iterdir()), [])

    def test_private_log_is_required_before_runtime_exec(self):
        label = self.remote.deploy(self.request)
        digest = label.split(".")[-1]
        log = self.root / "logs" / (digest + ".log")
        log.write_text("old output")
        log.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "managed log is unsafe"):
            self.remote.run(str(self.root), digest)

    def test_remote_bootstrap_reads_source_and_request_from_stdin(self):
        payload = (
            base64.b64encode(provider.REMOTE.encode())
            + b"\n"
            + json.dumps({"op": "wrong"}).encode()
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-c", provider.BOOT],
            input=payload,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ok"], False)
        self.assertNotIn(self.secret.encode(), result.stdout)


if __name__ == "__main__":
    unittest.main()