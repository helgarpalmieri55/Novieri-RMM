/**
 * Structured values instead of JSON blobs, and one way of saying "nothing".
 *
 * The fixtures are the audit's own evidence strings, copied from the DOM probe
 * it ran against /devices/:id, so a regression here fails against the actual
 * data that produced the finding rather than against a convenient invention.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Attributes, describeValue } from "./attributes";
import { EmptyState, NoValue } from "./emptystate";

const user = userEvent.setup();

// Verbatim from the audit: the longest of the 35 JSON cells it found, at 225
// characters.
const NIC = {
  mac: "3c:a8:2a:15:6b:11",
  name: "eno2",
  addresses: [
    { family: "ipv4", origin: "static", address: "172.22.104.28/29" },
    { family: "ipv6", origin: "link_local", address: "fe80::3ea8:2aff:fe15:6b11%eno2/64" },
  ],
};

const DISK = { model: "LOGICAL VOLUME", size_bytes: 256022937600 };

describe("Attributes", () => {
  it("renders a disk as labelled values, not as JSON", () => {
    render(<Attributes value={DISK} empty="No attributes recorded" />);
    // "for `disk` -> 'Model: LOGICAL VOLUME · Size: 238 GiB'" -- the fix note.
    expect(screen.getByText("LOGICAL VOLUME")).toBeInTheDocument();
    expect(screen.getByText("238 GiB")).toBeInTheDocument();
    expect(screen.getByText(/Size bytes/)).toBeInTheDocument();
  });

  it("prints no braces or quotes in the visible text", () => {
    // THE FINDING ITSELF, as an assertion: braces, quotes and escaped
    // interface suffixes are what the operator was being asked to read past.
    const { container } = render(<Attributes value={NIC} empty="No attributes recorded" raw={false} />);
    const shown = container.textContent ?? "";
    expect(shown).not.toContain("{");
    expect(shown).not.toContain('"');
    expect(shown.length).toBeLessThan(JSON.stringify(NIC).length);
  });

  it("keeps an address readable rather than expanding its record", () => {
    render(<Attributes value={NIC} empty="No attributes recorded" raw={false} />);
    expect(screen.getByText(/172\.22\.104\.28\/29/)).toBeInTheDocument();
    // `{family, origin, address}` is worth exactly one field; printing the
    // other two is the noise the audit measured.
    expect(screen.queryByText(/link_local/)).toBeNull();
  });

  it("keeps the raw object behind a closed disclosure for engineers", () => {
    // The fix note asks for this explicitly: "Keep a 'View raw' disclosure for
    // the JSON if engineers need it."
    render(<Attributes value={DISK} empty="No attributes recorded" />);
    const disclosure = screen.getByText("View raw");
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
  });

  it("shows the JSON once the disclosure is opened", async () => {
    render(<Attributes value={DISK} empty="No attributes recorded" />);
    await user.click(screen.getByText("View raw"));
    expect(screen.getByText(/"model": "LOGICAL VOLUME"/)).toBeInTheDocument();
  });

  it("says what is missing instead of rendering an empty object", () => {
    // "`{}` is developer output, not a user-facing empty state -- it reads as
    // 'something is broken' rather than 'this job hasn't produced a result
    // yet'."
    const { container } = render(
      <Attributes value={{}} empty="No result yet — this job hasn't run." />,
    );
    expect(container.textContent).not.toContain("{}");
    expect(screen.getByText("No result yet — this job hasn't run.")).toBeInTheDocument();
  });

  it("treats null and a non-object the same way", () => {
    render(<Attributes value={null} empty="Nothing recorded" />);
    expect(screen.getByText("Nothing recorded")).toBeInTheDocument();
  });
});

describe("describeValue", () => {
  it("formats a byte-shaped key and leaves other numbers alone", () => {
    expect(describeValue("size_bytes", 256022937600)).toBe("238 GiB");
    expect(describeValue("cores", 12)).toBe("12");
  });

  it("humanises an identifier-shaped string but not an address or a path", () => {
    expect(describeValue("state", "not_applicable")).toBe("Not applicable");
    expect(describeValue("address", "172.22.104.28/29")).toBe("172.22.104.28/29");
    expect(describeValue("mac", "3c:a8:2a:15:6b:11")).toBe("3c:a8:2a:15:6b:11");
    expect(describeValue("path", "/var/lib/itms")).toBe("/var/lib/itms");
  });

  it("says Yes and No rather than true and false", () => {
    expect(describeValue("enabled", true)).toBe("Yes");
    expect(describeValue("enabled", false)).toBe("No");
  });
});

describe("NoValue", () => {
  it("hides the dash from assistive tech and names the missing field", () => {
    // "Screen readers announce this as 'em dash' or skip it; sighted users
    // cannot tell which field is empty."
    const { container } = render(<NoValue label="No FQDN recorded" />);
    expect(container.querySelector('[aria-hidden="true"]')?.textContent).toBe("—");
    expect(screen.getByText("No FQDN recorded")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("says what is missing, why, and what to do about it", () => {
    render(
      <EmptyState
        icon="backup"
        title="No deployments recorded"
        body="This device has never been targeted by a deployment."
        action={<button type="button">Create a deployment</button>}
      />,
    );
    expect(screen.getByRole("heading", { name: "No deployments recorded" })).toBeInTheDocument();
    expect(screen.getByText(/never been targeted/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create a deployment" })).toBeInTheDocument();
  });

  it("renders without an action, deliberately rather than by omission", () => {
    render(<EmptyState icon="logs" title="Nothing yet" body="Records appear here once collected." />);
    expect(screen.getByRole("heading", { name: "Nothing yet" })).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps its glyph out of the accessibility tree", () => {
    // The icon repeats the heading; announcing it adds a second, wordless
    // stop before the sentence that matters.
    const { container } = render(
      <EmptyState icon="logs" title="Nothing yet" body="Records appear here once collected." />,
    );
    expect(container.querySelector(".empty-icon")).toHaveAttribute("aria-hidden", "true");
  });
});
