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
import tempfile
import types
import unittest
from unittest import mock

HERE = Path(__file__).parent
SOURCE = HERE.joinpath("buzz-backend-ssh")
loader = importlib.machinery.SourceFileLoader("provider", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
provider = importlib.util.module_from_spec(spec)
loader.exec_module(provider)

def helper():
    module = types.ModuleType("remote_helper")
    module.__dict__["_SOURCE"] = provider.REMOTE.encode()
    exec(compile(provider.REMOTE, "<remote>", "exec"), module.__dict__)
    return module

def nsec_from_one():
    alphabet = "023456789acdefghjklmnpqrstuvwxyz"
    values = []
    acc = bits = 0
    for byte in b"\0" * 31 + b"\1":
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    polymod = 1
    for n in [3, 3, 3, 3, 0, 14, 19, 5, 3] + values + [0] * 6:
        top = polymod >> 25
        polymod = ((polymod & 0x1ffffff) << 5) ^ n
        for i, gen in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if top >> i & 1:
                polymod ^= gen
    checksum = polymod ^ 1
    return "nsec1" + "".join(alphabet[n] for n in values + [(checksum >> (5 * (5 - i))) & 31 for i in range(6)])

class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.running = False
        self.calls = []
        self.fail_bootstrap = False
    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("print", "gui/" + str(os.getuid())):
            return subprocess.CompletedProcess(args, 0, "domain", "")
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113,
                                               "state = running" if self.running else "state = exited", "")
        if args[0] == "bootout":
            self.loaded = self.running = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "bootstrap":
            if self.fail_bootstrap:
                return subprocess.CompletedProcess(args, 1, "", "")
            self.loaded = self.running = True
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.remote = helper()
        self.secret = nsec_from_one()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".buzz-ssh-provider"
        self.fake = FakeLaunchctl()
        self.remote.launchctl = self.fake
        self.remote.shutil.which = lambda name: "/usr/bin/true" if name in ("buzz-acp", "codex-acp") else None
        self.request = {
            "op": "deploy",
            "provider_config": {"host": "example", "remote_directory": str(self.root)},
            "agent": {
                "relay_url": "wss://relay.example",
                "private_key_nsec": self.secret,
                "auth_tag": "fake-auth-tag",
                "provider": "openai",
                "launch": {"command": "codex-acp", "args": [],
                           "owner_pubkey": "a" * 64,
                           "env": {"MODEL": "test"},
                           "policy_env": {"BUZZ_ACP_AGENTS": "1"}}
            }
        }
    def test_info_and_single_file_staging(self):
        staged = Path(self.temp.name) / "provider"
        shutil.copy2(SOURCE, staged)
        result = subprocess.run([str(staged)], input=b'{"op":"info"}', capture_output=True)
        self.assertEqual(result.returncode, 0)
        info = json.loads(result.stdout)
        self.assertEqual(info["protocol_version"], 1)
        self.assertEqual(set(info["config_schema"]["required"]), {"host", "remote_directory"})
        self.assertEqual(set(info["config_schema"]["properties"]), {"host", "remote_directory", "runtime_command"})
    def test_identity_and_owner_fail_before_writes(self):
        self.assertEqual(self.remote.pubkey(self.remote.decode_nsec(self.secret)),
                         f"{self.remote.GX:064x}")
        for field, value in (("private_key_nsec", "not-a-key"), ("relay_url", ""),):
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
        self.assertTrue(first.startswith("local.buzz.ssh."))
        self.assertEqual(self.remote.deploy(self.request), first)
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 1)
        state = self.root / "state" / (first.split(".")[-1] + ".json")
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        plist = plistlib.loads((self.root / "jobs" / (first + ".plist")).read_bytes())
        self.assertFalse(plist["KeepAlive"])
        self.assertNotIn(self.secret, str(plist))
        changed = json.loads(json.dumps(self.request))
        changed["agent"]["launch"]["env"]["MODEL"] = "different"
        with self.assertRaisesRegex(ValueError, "stop it before redeploy"):
            self.remote.deploy(changed)
        self.assertIn(self.secret, state.read_text())
    def test_stopped_job_can_restart_and_failed_bootstrap_rolls_back(self):
        label = self.remote.deploy(self.request)
        self.fake.running = False
        self.assertEqual(self.remote.deploy(self.request), label)
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 2)
        previous = (self.root / "state" / (label.split(".")[-1] + ".json")).read_bytes()
        self.fake.running = False
        self.fake.fail_bootstrap = True
        changed = json.loads(json.dumps(self.request))
        changed["agent"]["launch"]["env"]["MODEL"] = "different"
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            self.remote.deploy(changed)
        self.assertEqual((self.root / "state" / (label.split(".")[-1] + ".json")).read_bytes(), previous)
    def test_remote_command_cannot_be_client_path(self):
        req = json.loads(json.dumps(self.request))
        req["agent"]["launch"]["command"] = "/Applications/Some.app/Contents/MacOS/codex-acp"
        with self.assertRaisesRegex(ValueError, "remote PATH"):
            self.remote.deploy(req)
        self.assertFalse(self.root.exists())
    def test_concurrent_identical_deploy_has_one_bootstrap(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            labels = list(pool.map(self.remote.deploy, [self.request, self.request]))
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.fake.calls), 1)
    def test_remote_error_cannot_echo_a_secret(self):
        response = json.dumps({"ok": False, "error": self.secret}).encode()
        with mock.patch.object(provider.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, response, b"")
            result = provider.respond(self.request)
        self.assertEqual(result, {"ok": False, "error": "remote deployment refused"})
    def test_missing_remote_runtime_refuses_before_writes(self):
        self.remote.shutil.which = lambda name: None
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
    def test_failed_new_bootstrap_does_not_unload_another_job(self):
        self.fake.fail_bootstrap = True
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            self.remote.deploy(self.request)
        self.assertFalse(any(call[0] == "bootout" for call in self.fake.calls))
        self.assertEqual(list((self.root / "state").iterdir()), [])
    def test_private_log_is_required_before_runtime_exec(self):
        label = self.remote.deploy(self.request)
        digest = label.split(".")[-1]
        log = self.root / "logs" / (digest + ".log")
        log.write_text("old output")
        log.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "managed log is unsafe"):
            self.remote.run(str(self.root), digest)
    def test_remote_bootstrap_reads_source_and_request_from_stdin(self):
        payload = base64.b64encode(provider.REMOTE.encode()) + b"\n" + json.dumps({"op": "wrong"}).encode()
        result = subprocess.run(["/usr/bin/python3", "-c", provider.BOOT], input=payload,
                                capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ok"], False)
        self.assertNotIn(self.secret.encode(), result.stdout)

if __name__ == "__main__":
    unittest.main()
