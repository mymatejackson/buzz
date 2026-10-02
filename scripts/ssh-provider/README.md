# SSH provider prototype

This is a single-file buzz-backend-ssh provider for Buzz protocol v1.
Buzz stages exactly one executable for info and deploy; the embedded helper
is sent over SSH stdin. It uses an existing OpenSSH host alias and a native
macOS launchd user job. It does not impersonate Kubernetes or run a second
Buzz desktop or relay.

## Scope

- Info is pure and exposes host, remote_directory, and runtime_command in
  Run on. The provider must be on the development desktop process PATH.
- Deploy uses SSH BatchMode=yes and StrictHostKeyChecking=yes. It never
  installs a host key, changes SSH config, copies a credential file, or starts
  an SSH daemon. An untrusted or unreachable host fails.
- The remote directory must be an existing project directory's
  .buzz-ssh-provider child. The provider creates this child with mode 0700.
  Its helper, launchd plist, state, workspace, and logs stay beneath it.
  The JSON state contains the deployed identity and is mode 0600; it must
  never be committed, copied into a public report, or treated as a backup.
- One label is derived from relay URL and the key public identity. A
  per-label file lock serializes concurrent deploys. Identical running
  deployments return the same label; changed running configuration is refused
  until the agent stops. A bootstrap failure restores prior managed files.
- The launchd plist contains only the helper path and nonsecret ID. It has
  RunAtLoad=true and KeepAlive=false. A harness exit, including an
  owner-authorized shutdown, stays stopped. Pressing Start again bootstraps
  the stopped job. This prototype does not claim crash restart or reboot
  persistence.
- No live identity, SSH connection, or launchd service is used by the tests.
  The runtime and ACP command are resolved on the remote host. The runtime
  can be a remote absolute executable path; the desktop-resolved ACP
  launch.command must be a portable command name.

## Isolated development identity

The stock Buzz source already has a named demo build identity. This prototype
uses that path rather than the ordinary dev identifier: its Tauri bundle and
app data identifier, Keychain service, deep link scheme, CLI name, and nest
name are distinct. A repo-local preparation command writes only ignored,
mode-0600 config files under .cache/ssh-provider:

    node scripts/ssh-provider/prepare-isolated-demo.mjs

The generated slug must be supplied as BUZZ_BUILD_DEMO_SLUG at compilation
together with the generated Tauri config. The config file alone is not
isolation. The preparation command refuses inherited private-key and
identity-sharing environment variables. Buzz migration code now explicitly
skips production-to-dev repos and key imports for named demo builds even if a
future config gives one a dev-shaped identifier. No GUI has been launched.

## Offline check

Run from the repository root:

    node --test desktop/scripts/demo-build-config.test.mjs scripts/ssh-provider/prepare-isolated-demo.test.mjs
    PYTHONPYCACHEPREFIX="$PWD/.cache/pycache" /usr/bin/python3 -m unittest discover -s scripts/ssh-provider -p 'test_*.py' -v
    printf '{"op":"info"}' | scripts/ssh-provider/buzz-backend-ssh

Tests use temporary directories and a fake launchctl. They cover wire info,
identity and owner refusal, strict SSH options and argument safety, remote
bootstrap, one-job duplicate suppression, changed-running refusal, restart
after stop, rollback, symlink refusal, missing runtime, and remote error
redaction.

## Required before a live trial

1. Verify the compiled development build uses the prepared demo slug and
   Tauri config. Do not open a client using the ordinary dev identity or the
   stock desktop account store.
2. Confirm an existing SSH alias, trusted host key, BatchMode access, launchd
   GUI domain, remote Python 3, runtime and ACP command on the target. Provision
   or copy no credentials in this step.
3. Review exactly where the live nsec and auth tag will come from, how the
   private remote state will be protected, and how to cleanly disable or archive
   it. Approval is required before sending a live identity.
4. Prove an owner shutdown leaves the launchd job stopped, Start restores
   the same identity, and desktop close/reopen preserves both relay presence
   and conversation history. Check the local device managed deployment
   record separately from the relay community data.

The current desktop provider protocol has info and deploy, but no undeploy
operation. Status comes from relay presence. Model discovery and harness
selection currently probe the local desktop, so this prototype does not yet
provide host-aware model lists or complete edit/stop/delete parity. Those
need small capability-gated desktop changes and a defined remote
configuration/restart path.

## Separate-client test sequence

On a spare Mac, first install and inspect the stock client against the
existing community and chat without changing agent management. Next, run an
isolated development client with a separate app identity and empty data
namespace, and verify the provider appears in Run on. Finally, after explicit
approval for live identity handling, test start, stop, restart, edit refusal
while running, and recovery on a disposable identity. Compare what the two
clients share through the relay with each client local managed-agent record.
The desktop stores managed-agents.json under its own app data directory and
stores agent keys in its own Keychain service. The backend_agent_id is part of
that local record; conversation and presence flow through the relay. A second
client seeing the same community and chat does not thereby own or administer
the first client local deployment. Do not infer cross-device administration
from shared chat history.
