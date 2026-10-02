#!/usr/bin/env node
// Prepare only a project-local Tauri config. This does not build or launch Buzz.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { demoBuildConfig, productionBuildIdentity } from "../../desktop/scripts/demo-build-config.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(SCRIPT_DIR, "../..");
const DISALLOWED_ENV = [
  "BUZZ_PRIVATE_KEY",
  "NOSTR_PRIVATE_KEY",
  "BUZZ_SHARE_IDENTITY",
  "BUZZ_DEV_KEYRING_SERVICE",
];

export function isolatedPrototypeConfig(repoRoot, environment = process.env) {
  for (const name of DISALLOWED_ENV) {
    if (Object.hasOwn(environment, name)) {
      throw new Error(name + " must be absent for an isolated prototype build");
    }
  }
  const buildId = createHash("sha256")
    .update("buzz-ssh-provider-prototype\0")
    .update(resolve(repoRoot))
    .digest("hex")
    .slice(0, 16);
  const config = demoBuildConfig("SSH Provider Prototype", buildId);
  assert.notEqual(config.identifier, productionBuildIdentity.identifier);
  assert.notEqual(config.keyringService, productionBuildIdentity.keyringService);
  assert.notEqual(config.nestName, productionBuildIdentity.nestName);
  assert.match(config.identifier, /^xyz\.block\.buzz\.app\.demo\./);
  assert.match(config.keyringService, /^buzz-desktop-demo\./);
  return config;
}

export function prepare(repoRoot = REPO_ROOT, environment = process.env) {
  const config = isolatedPrototypeConfig(repoRoot, environment);
  const outputDir = join(repoRoot, ".cache", "ssh-provider");
  mkdirSync(outputDir, { recursive: true, mode: 0o700 });
  const configPath = join(outputDir, "tauri-demo.conf.json");
  const identityPath = join(outputDir, "build-identity.json");
  writeFileSync(configPath, JSON.stringify(config.tauriConfig, null, 2) + "\n", { mode: 0o600 });
  writeFileSync(identityPath, JSON.stringify({
    slug: config.slug,
    identifier: config.identifier,
    appDataIdentity: config.appDataIdentity,
    keyringService: config.keyringService,
    nestName: config.nestName,
    deepLinkScheme: config.deepLinkScheme,
    providerExecutable: join(repoRoot, "scripts", "ssh-provider", "buzz-backend-ssh"),
  }, null, 2) + "\n", { mode: 0o600 });
  return { configPath, identityPath, config };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const { configPath, identityPath, config } = prepare();
    console.log(JSON.stringify({
      configPath, identityPath,
      identifier: config.identifier,
      keyringService: config.keyringService,
      slug: config.slug,
    }));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
