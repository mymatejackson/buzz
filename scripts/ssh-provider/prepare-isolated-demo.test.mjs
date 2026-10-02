import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { isolatedPrototypeConfig, prepare } from "./prepare-isolated-demo.mjs";

test("prototype identity is stable per checkout and distinct across checkouts", () => {
  const first = isolatedPrototypeConfig("/tmp/checkout-a", {});
  const again = isolatedPrototypeConfig("/tmp/checkout-a", {});
  const second = isolatedPrototypeConfig("/tmp/checkout-b", {});
  assert.deepEqual(first, again);
  for (const key of ["identifier", "appDataIdentity", "keyringService", "nestName", "deepLinkScheme"]) {
    assert.notEqual(first[key], second[key], key);
  }
  assert.notEqual(first.identifier, "xyz.block.buzz.app");
  assert.notEqual(first.keyringService, "buzz-desktop");
  assert.notEqual(first.keyringService, "buzz-desktop-dev");
  assert.notEqual(first.nestName, ".buzz");
  assert.notEqual(first.nestName, ".buzz-dev");
});

test("prototype preparation refuses inherited identity and sharing environment", () => {
  for (const key of ["BUZZ_PRIVATE_KEY", "NOSTR_PRIVATE_KEY", "BUZZ_SHARE_IDENTITY", "BUZZ_DEV_KEYRING_SERVICE"]) {
    assert.throws(() => isolatedPrototypeConfig("/tmp/checkout", { [key]: "test" }), new RegExp(key));
  }
});

test("preparation writes config and identity only under checkout cache", () => {
  const root = mkdtempSync(join(tmpdir(), "buzz-ssh-isolation-"));
  try {
    const result = prepare(root, {});
    assert.ok(result.configPath.startsWith(join(root, ".cache", "ssh-provider")));
    assert.ok(result.identityPath.startsWith(join(root, ".cache", "ssh-provider")));
    const tauri = JSON.parse(readFileSync(result.configPath, "utf8"));
    const metadata = JSON.parse(readFileSync(result.identityPath, "utf8"));
    assert.equal(tauri.identifier, metadata.identifier);
    assert.equal(tauri.productName, "Buzz SSH Provider Prototype");
    assert.equal(metadata.keyringService, result.config.keyringService);
    assert.equal(statSync(result.configPath).mode & 0o777, 0o600);
    assert.equal(statSync(result.identityPath).mode & 0o777, 0o600);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
