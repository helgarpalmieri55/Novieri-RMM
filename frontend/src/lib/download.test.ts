/**
 * The saving half of an authenticated download. The AUTHENTICATED half is
 * asserted where it can be — at the pages, which must go through the API
 * client rather than through a bare anchor (see `DevicesPage.test.tsx` and
 * `SoftwarePage.test.tsx`).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { saveBlob } from "./download";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("saveBlob", () => {
  it("follows an object URL for the bytes under the name it was given", () => {
    const createObjectURL = vi.fn().mockReturnValue("blob:it-suite/1");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const clicked: HTMLAnchorElement[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push(this);
      });

    const blob = new Blob(["hostname\n"]);
    saveBlob(blob, "devices.xlsx");

    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalledOnce();
    expect(clicked[0]?.download).toBe("devices.xlsx");
    expect(clicked[0]?.getAttribute("href")).toBe("blob:it-suite/1");
  });

  it("is in the document when it is clicked, and out of it afterwards", () => {
    // A detached anchor's click is a no-op in Firefox, and an anchor left
    // behind is a stray node in the page for every export of the session.
    Object.assign(URL, {
      createObjectURL: vi.fn().mockReturnValue("blob:it-suite/2"),
      revokeObjectURL: vi.fn(),
    });
    let connectedAtClick: boolean | undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      connectedAtClick = this.isConnected;
    });

    saveBlob(new Blob(["x"]), "software.xlsx");

    expect(connectedAtClick).toBe(true);
    expect(document.querySelector("a[download]")).toBeNull();
  });

  it("revokes the object URL, so a long session does not pin every export", () => {
    const revokeObjectURL = vi.fn();
    Object.assign(URL, {
      createObjectURL: vi.fn().mockReturnValue("blob:it-suite/3"),
      revokeObjectURL,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    saveBlob(new Blob(["x"]), "devices.xlsx");

    expect(revokeObjectURL).toHaveBeenCalledWith("blob:it-suite/3");
  });
});
