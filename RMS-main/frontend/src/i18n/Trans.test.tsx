import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setActiveLanguage } from "./core";
import { Trans } from "./Trans";

describe("Trans", () => {
  afterEach(() => setActiveLanguage("en"));

  it("turns a known marker into the component with the inner text", () => {
    const { container } = render(
      <Trans k="common.help.docsLink" components={{ link: <a href="/guide" /> }} />,
    );
    expect(container.innerHTML).toBe('See <a href="/guide">the guide</a> for details.');
  });

  it("follows the active language", () => {
    setActiveLanguage("es");
    const { container } = render(
      <Trans k="common.help.docsLink" components={{ link: <a href="/guide" /> }} />,
    );
    expect(container.innerHTML).toBe('Consulte <a href="/guide">la guía</a> para más detalles.');
  });

  it("renders an unknown marker as plain text, with no stray tag-name node", () => {
    const { container } = render(<Trans k="common.help.docsLink" components={{ other: <b /> }} />);
    expect(container.textContent).toBe("See <link>the guide</link> for details.");
    expect(container.querySelector("b")).toBeNull();
  });

  it("renders text without markers unchanged", () => {
    const { container } = render(<Trans k="common.actions.save" components={{}} />);
    expect(container.innerHTML).toBe("Save");
  });

  it("handles two adjacent markers", () => {
    const { container } = render(
      <Trans k="common.help.twoLinks" components={{ a: <i />, b: <b /> }} />,
    );
    expect(container.innerHTML).toBe("<i>One</i><b>Two</b>");
  });

  it("fills a value inside a marker, and a value that looks like a marker stays text", () => {
    const { container } = render(
      <Trans
        k="patch.devicesPanel.open"
        vars={{ hostname: "pc-01" }}
        components={{ link: <a href="/devices/1" /> }}
      />,
    );
    expect(container.innerHTML).toBe(
      '<a href="/devices/1">Open pc-01</a> for its reboot state, job history and inventory.',
    );

    cleanup();
    const spoof = render(
      <Trans
        k="patch.devicesPanel.open"
        vars={{ hostname: "x</link> or <link>evil" }}
        components={{ link: <a href="/devices/1" /> }}
      />,
    );
    expect(spoof.container.querySelectorAll("a")).toHaveLength(1);
    expect(spoof.container.querySelector("a")?.textContent).toBe("Open x</link> or <link>evil");
  });
});
