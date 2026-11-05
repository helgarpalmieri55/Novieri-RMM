#!/usr/bin/env node
/**
 * Falsifier 4 of the own-authentication design (§10), in a real browser:
 * "Una llave de acceso sin verificación del usuario se rechaza. En la CI, con
 * el autenticador virtual de Chromium."
 *
 * Two ceremonies, each against options py_webauthn generates and judged by
 * py_webauthn with `require_user_verification=True`
 * (`backend/tests/webauthn_uv_probe.py`), the argument the platform passes:
 *
 *   1. CONTROL. An authenticator that verifies the user, options asking for it:
 *      the response carries UV=1 and must be ACCEPTED. Without it, step 2
 *      could pass because nothing verifies at all.
 *   2. FALSIFIER. An authenticator that cannot verify the user, and a client
 *      that rewrote the options to `userVerification: "discouraged"` -- what a
 *      hostile page or extension can do, since the server never sees the
 *      options the browser used. The response carries UV=0 and must be
 *      REFUSED, for that reason and no other.
 *
 * The page is http://localhost:<port>/, a secure context (WebAuthn requires
 * one) whose RP ID is "localhost". Playwright is resolved from a global
 * install, as scripts/a11y-browser.mjs resolves it, and for the same reason.
 *
 * Two authenticators rather than `WebAuthn.setUserVerified` on one: with UV
 * required and verification failing, Chromium rejects the ceremony in the
 * browser, so no UV=0 response would ever reach the verifier.
 */
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const BACKEND = resolve(FRONTEND, "..", "backend");
const PYTHON = process.env.ITS_PYTHON ?? join(BACKEND, ".venv", "bin", "python");
const require = createRequire(import.meta.url);
const GLOBAL_MODULES = join(dirname(dirname(process.execPath)), "lib/node_modules");

async function loadPlaywright() {
  for (const spec of [
    "playwright",
    join(GLOBAL_MODULES, "playwright/index.mjs"),
    "/opt/node22/lib/node_modules/playwright/index.mjs",
    "/usr/lib/node_modules/playwright/index.mjs",
    "/usr/local/lib/node_modules/playwright/index.mjs",
  ]) {
    try {
      const mod = await import(spec.startsWith("/") ? spec : require.resolve(spec, { paths: [FRONTEND] }));
      return mod.chromium ? mod : mod.default;
    } catch {
      /* try the next one */
    }
  }
  throw new Error("playwright is not installed: `npm i -g playwright && npx playwright install chromium`");
}

function probe(args, input) {
  const run = spawnSync(PYTHON, ["-m", "tests.webauthn_uv_probe", ...args], {
    cwd: BACKEND,
    input: input === undefined ? undefined : JSON.stringify(input),
    encoding: "utf8",
  });
  if (run.status !== 0) throw new Error(`probe ${args[0]} exited ${run.status}: ${run.stderr}`);
  return JSON.parse(run.stdout);
}

/** Runs IN THE PAGE: W3C options JSON in, the registration response JSON out. */
async function register(optionsJSON) {
  const toBuffer = (text) => {
    const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
  };
  const toText = (buffer) =>
    btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const publicKey = {
    ...optionsJSON,
    challenge: toBuffer(optionsJSON.challenge),
    user: { ...optionsJSON.user, id: toBuffer(optionsJSON.user.id) },
    excludeCredentials: (optionsJSON.excludeCredentials ?? []).map((c) => ({ ...c, id: toBuffer(c.id) })),
  };
  const credential = await navigator.credentials.create({ publicKey });
  return {
    id: credential.id,
    rawId: toText(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toText(credential.response.clientDataJSON),
      attestationObject: toText(credential.response.attestationObject),
      transports: credential.response.getTransports ? credential.response.getTransports() : [],
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

const AUTHENTICATOR = {
  protocol: "ctap2",
  ctap2Version: "ctap2_1",
  transport: "internal",
  hasResidentKey: true,
  automaticPresenceSimulation: true,
};

async function main() {
  const server = createServer((_, response) => {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end("<!doctype html><title>webauthn-uv</title>");
  });
  await new Promise((done) => server.listen(0, done));
  const origin = `http://localhost:${server.address().port}`;
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const failures = [];
  try {
    const page = await browser.newPage();
    await page.goto(`${origin}/`);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("WebAuthn.enable", { enableUI: false });

    const verifying = await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: { ...AUTHENTICATOR, hasUserVerification: true, isUserVerified: true },
    });
    const asked = probe(["options", "localhost", "required"]);
    const control = probe(["verify", origin, "localhost"], {
      options: asked,
      credential: await page.evaluate(register, asked),
    });
    if (!control.accepted) failures.push(`control: a user-verified passkey was refused (${control.error})`);
    await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: verifying.authenticatorId });

    await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: { ...AUTHENTICATOR, hasUserVerification: false },
    });
    const issued = probe(["options", "localhost", "required"]);
    const downgraded = {
      ...issued,
      authenticatorSelection: { ...issued.authenticatorSelection, userVerification: "discouraged" },
    };
    const outcome = probe(["verify", origin, "localhost"], {
      options: issued,
      credential: await page.evaluate(register, downgraded),
    });
    if (outcome.accepted) {
      failures.push("falsifier: a passkey created WITHOUT user verification was accepted");
    } else if (!/User verification is required/.test(outcome.error)) {
      failures.push(`falsifier: refused, but not for user verification (${outcome.error}); it proves nothing`);
    }
  } finally {
    await browser.close();
    server.close();
  }
  for (const line of failures) console.log(`FAIL  ${line}`);
  if (failures.length > 0) process.exit(1);
  console.log("ok    a user-verified passkey is accepted; one without user verification is refused");
}

await main();
