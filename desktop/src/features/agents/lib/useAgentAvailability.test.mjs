import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { resolveAgentAvailability } from "./useAgentAvailability.ts";
import {
  getManagedAgentPrimaryActionLabel,
  getManagedAgentPrimaryActionState,
  isManagedAgentActive,
} from "./managedAgentControlActions.ts";
import { AgentRuntimeAvatarControl } from "../ui/AgentRuntimeAvatarControl.tsx";

const deployed = {
  status: "deployed",
  backend: { type: "provider", id: "fixture" },
  backendAgentId: "retained-receipt",
};

for (const [presence, expectedAction, expectedLabel] of [
  ["online", "stop", "Shutdown"],
  ["away", "stop", "Shutdown"],
  ["offline", "start", "Start"],
  [undefined, "start", "Start"],
]) {
  test(`retained deployment receipt routes from established availability (${presence})`, () => {
    const availability = resolveAgentAvailability(presence, true, true);
    assert.equal(availability, presence ?? "offline");
    assert.equal(isManagedAgentActive(deployed), true);
    const primaryAction = getManagedAgentPrimaryActionState(
      deployed,
      availability,
    );
    assert.equal(primaryAction.action, expectedAction);
    assert.equal(
      getManagedAgentPrimaryActionLabel(deployed, availability),
      expectedLabel,
    );
    const html = renderToStaticMarkup(
      createElement(AgentRuntimeAvatarControl, {
        activeTestId: "active",
        isActive: primaryAction.action !== "start",
        availability,
        isStarting: false,
        label: "Agent",
        startTestId: "start",
        onStart() {},
      }),
    );
    assert.doesNotMatch(html, /is running/);
    if (expectedAction === "start") {
      assert.match(html, /data-testid="start"/);
    } else {
      assert.match(
        html,
        new RegExp(
          `Agent: ${availability[0].toUpperCase()}${availability.slice(1)}`,
        ),
      );
      assert.doesNotMatch(html, /data-testid="start"/);
    }
  });
}

for (const [loaded, connected] of [
  [false, true],
  [true, false],
  [false, false],
]) {
  test(`unavailable presence is unknown, not cached online (${loaded}, ${connected})`, () => {
    const availability = resolveAgentAvailability("online", loaded, connected);
    assert.equal(availability, undefined);
    const primaryAction = getManagedAgentPrimaryActionState(
      deployed,
      availability,
    );
    assert.equal(primaryAction.action, null);
    assert.equal(primaryAction.label, "Availability unknown");
    assert.match(primaryAction.blockReason, /Reconnect to the relay/);
    const html = renderToStaticMarkup(
      createElement(AgentRuntimeAvatarControl, {
        activeTestId: "active",
        isActive: primaryAction.action !== "start",
        availability,
        isStarting: false,
        label: "Agent",
        startTestId: "start",
        onStart() {},
      }),
    );
    assert.match(html, /Availability unknown/);
    assert.doesNotMatch(html, /bg-emerald-500|is running/);
  });
}

for (const lifecycle of ["running", "stopped"]) {
  test(`local ${lifecycle} controls remain independent of online presence`, () => {
    const agent = { status: lifecycle, backend: { type: "local" } };
    const isActive = isManagedAgentActive(agent);
    assert.equal(
      getManagedAgentPrimaryActionLabel(agent, undefined),
      isActive ? "Stop" : "Start agent",
    );
    const html = renderToStaticMarkup(
      createElement(AgentRuntimeAvatarControl, {
        activeTestId: "active",
        isActive,
        availability: "online",
        isStarting: false,
        label: "Local Agent",
        startTestId: "start",
        onStart() {},
      }),
    );
    assert.equal(html.includes('data-testid="start"'), false);
    assert.equal(html.includes('data-testid="active"'), true);
  });
}

for (const availability of ["online", "away"]) {
  test(`stale restart and runtime error cannot hide stopped ${availability} presence`, () => {
    const html = renderToStaticMarkup(
      createElement(AgentRuntimeAvatarControl, {
        activeTestId: "active",
        startTestId: "start",
        errorTestId: "error",
        isActive: false,
        isStarting: false,
        requiresRestart: true,
        errorLabel: "Previous startup failed",
        availability,
        label: "Agent",
        onStart() {},
      }),
    );
    assert.match(html, /data-testid="active"/);
    assert.doesNotMatch(
      html,
      /data-testid="start"|data-testid="error"|<button/,
    );
  });
}
