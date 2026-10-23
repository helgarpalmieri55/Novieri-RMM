/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true, ws: true },
      // ANCHORED, BECAUSE A PROXY KEY IS A PREFIX AND `/enrollment` STARTS
      // WITH `/enroll`. The plain string key matched the CONSOLE'S OWN
      // enrollment page and forwarded it to the backend, which answers 404
      // for it -- or, with no backend running, 502. The page was unreachable
      // by URL in dev for as long as this rule has existed, and the harmony
      // gate reported it as "/enrollment failed to load" on every run.
      // A key beginning with `^` is read as a RegExp by Vite, so this matches
      // `/enroll` and `/enroll/...` (the agent's `POST /enroll/v1`) and
      // nothing else. Adding a sibling route later needs the same care: the
      // console has a page for almost every noun the API has an endpoint for.
      "^/enroll(/|$)": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // One setup file for the whole suite -- jest-dom, render cleanup, and
    // the unexpected-`console.error`-fails-the-test gate all apply
    // everywhere now. See `src/test/setup.ts`'s own comment for why this
    // used to be split into two `test.projects` (a `console-error-gated`
    // project scoped to `field.test.tsx` + `settings/**`, and a `default`
    // project for everything else) and why that split is gone.
    setupFiles: ["src/test/setup.ts"],
  },
});
