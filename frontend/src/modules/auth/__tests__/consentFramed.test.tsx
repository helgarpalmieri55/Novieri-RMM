/**
 * The consent page refuses to draw inside another page (B3-16): the
 * frame-busting fallback of RFC 6819 for a browser without CSP. The server's
 * `frame-ancestors 'none'` is `backend/tests/test_deploy_frame_headers.py`'s.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi, calls, installDefaults, renderAuth } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));
vi.mock("../consent", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../consent")>()),
  isFramed: () => true,
}));

beforeEach(() => {
  installDefaults();
});

describe("the consent page inside a frame", () => {
  it("draws no request and asks the server nothing", async () => {
    renderAuth("/oauth/authorize?client_id=itms_x");
    expect(await screen.findByRole("heading", { name: "Open this page on its own" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(calls.oauthAuthorizeDetails).not.toHaveBeenCalled();
  });
});
