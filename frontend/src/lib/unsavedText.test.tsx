/**
 * What a person had typed when their session ended, kept only so it can be
 * copied (operator decision 3, 2026-11-05, "Ocultar y vaciar"). PASSWORD
 * INPUTS ARE NEVER KEPT, and neither is any other secret typed in clear: a
 * one-time code, a recovery code, a license key whose mask was lifted.
 *
 * Driven through `userEvent`, which fires the browser's `input` events: a
 * field filled by the page itself (a seed from the server, a programmatic
 * value) was not typed by anyone and is never kept.
 *
 * DENY BY DEFAULT (final re-review, I3 and concern 4; coordinator decision):
 * only a `<textarea>`, or an input marked `data-keep`, is ever kept. Every
 * other single-line box -- the remote screen's "Text to type on the device",
 * through which a technician sends a device's admin password, among them --
 * is not, whoever forgot to mark it. RE-POINTED: the plain inputs these tests
 * keep are marked `data-keep` now.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { watchTyping, type TypingWatch } from "./unsavedText";

let watch: TypingWatch | null = null;

afterEach(() => {
  watch?.stop();
  watch = null;
});

function Controlled({ label, initial = "", type, keep = false }: { label: string; initial?: string; type?: string; keep?: boolean }) {
  const [value, setValue] = useState(initial);
  return (
    <label>
      <span>{label}</span>
      <input type={type} data-keep={keep || undefined} value={value} onChange={(event) => setValue(event.target.value)} />
    </label>
  );
}

/** A license key's "Show" toggle: the same box, a password box until it is pressed. */
function Revealable() {
  const [shown, setShown] = useState(false);
  const [value, setValue] = useState("");
  return (
    <div>
      <label>
        <span>Key</span>
        <input type={shown ? "text" : "password"} value={value} onChange={(event) => setValue(event.target.value)} />
      </label>
      <button type="button" onClick={() => setShown((on) => !on)}>
        Show
      </button>
    </div>
  );
}

describe("the text a person typed", () => {
  it("is kept with its field's label, in the order the fields are on the page", async () => {
    const user = userEvent.setup();
    render(
      <form>
        <label>
          <span>Reply</span>
          <textarea defaultValue="" />
        </label>
        <Controlled label="Subject" keep />
      </form>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Subject"), "Printer on floor 3");
    await user.type(screen.getByLabelText("Reply"), "Restarted the spooler.");

    expect(watch.keep()).toEqual([
      { label: "Reply", text: "Restarted the spooler." },
      { label: "Subject", text: "Printer on floor 3" },
    ]);
  });

  it("is not what the page filled in by itself, nor a box emptied again", async () => {
    const user = userEvent.setup();
    render(
      <>
        <Controlled label="Hostname" initial="laptop-7.corp.example" keep />
        <Controlled label="Note" keep />
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Note"), "x");
    await user.clear(screen.getByLabelText("Note"));

    expect(watch.keep()).toEqual([]);
  });

  it("names a field with no label by its accessible name, then by nothing", async () => {
    const user = userEvent.setup();
    render(
      <>
        <input aria-label="Search the fleet" data-keep />
        <textarea />
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Search the fleet"), "dc01");
    await user.type(screen.getByRole("textbox", { name: "" }), "loose words");

    expect(watch.keep()).toEqual([
      { label: "Search the fleet", text: "dc01" },
      { label: "", text: "loose words" },
    ]);
  });

  it("is nothing from before the watch started, or after it stopped", async () => {
    const user = userEvent.setup();
    render(<Controlled label="Note" keep />);
    await user.type(screen.getByLabelText("Note"), "before");
    watch = watchTyping(document);
    expect(watch.keep()).toEqual([]);
    watch.stop();
    await user.type(screen.getByLabelText("Note"), " after");
    expect(watch.keep()).toEqual([]);
  });
});

describe("deny by default (final re-review, I3)", () => {
  it("keeps a textarea and an input marked data-keep, and no other box", async () => {
    const user = userEvent.setup();
    render(
      <>
        <Controlled label="Ticket subject" keep />
        <Controlled label="Text to type on the device" />
        <Controlled label="Search" type="search" />
        <label>
          <span>Reply</span>
          <textarea />
        </label>
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Ticket subject"), "Printer offline");
    await user.type(screen.getByLabelText("Text to type on the device"), "Adm1n!Passw0rd");
    await user.type(screen.getByLabelText("Search"), "dc01");
    await user.type(screen.getByLabelText("Reply"), "Restarted it.");

    const kept = watch.keep();
    expect(kept).toEqual([
      { label: "Ticket subject", text: "Printer offline" },
      { label: "Reply", text: "Restarted it." },
    ]);
    expect(JSON.stringify(kept)).not.toContain("Adm1n");
  });

  it("never keeps a secret, even marked data-keep or typed into a textarea", async () => {
    const user = userEvent.setup();
    render(
      <>
        <label>
          <span>Private key</span>
          <textarea data-secret />
        </label>
        <label>
          <span>Code</span>
          <input data-keep autoComplete="one-time-code" />
        </label>
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Private key"), "-----BEGIN");
    await user.type(screen.getByLabelText("Code"), "123456");
    expect(watch.keep()).toEqual([]);
  });
});

describe("a secret typed into the page", () => {
  it("is never kept from a password box", async () => {
    const user = userEvent.setup();
    render(
      <>
        <Controlled label="Account" keep />
        <Controlled label="Password" type="password" keep />
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Account"), "CORP\\svc-push");
    await user.type(screen.getByLabelText("Password"), "hunter2-hunter2");

    const kept = watch.keep();
    expect(kept).toEqual([{ label: "Account", text: "CORP\\svc-push" }]);
    expect(JSON.stringify(kept)).not.toContain("hunter2");
  });

  it("is never kept from a password box whose mask was lifted after it was typed", async () => {
    const user = userEvent.setup();
    render(<Revealable />);
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Key"), "AAAA-BBBB-CCCC");
    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByLabelText("Key")).toHaveAttribute("type", "text");

    expect(watch.keep()).toEqual([]);
  });

  it("is never kept from a password box focused while masked, then shown and typed into", async () => {
    const user = userEvent.setup();
    render(<Revealable />);
    watch = watchTyping(document);
    await user.click(screen.getByLabelText("Key"));
    await user.click(screen.getByRole("button", { name: "Show" }));
    await user.type(screen.getByLabelText("Key"), "AAAA-BBBB-CCCC");
    expect(screen.getByLabelText("Key")).toHaveAttribute("type", "text");

    expect(watch.keep()).toEqual([]);
  });

  it("is never kept from a box typed into while shown and masked again", async () => {
    const user = userEvent.setup();
    render(<Revealable />);
    watch = watchTyping(document);
    await user.click(screen.getByRole("button", { name: "Show" }));
    await user.type(screen.getByLabelText("Key"), "AAAA-BBBB-CCCC");
    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByLabelText("Key")).toHaveAttribute("type", "password");

    expect(watch.keep()).toEqual([]);
  });

  it("is never kept from a box marked as a secret, nor from a code or a password a browser would fill", async () => {
    const user = userEvent.setup();
    render(
      <>
        <label>
          <span>Recovery code</span>
          <input data-secret autoComplete="off" />
        </label>
        <label>
          <span>Code</span>
          <input autoComplete="one-time-code" />
        </label>
        <label>
          <span>New password</span>
          <input autoComplete="new-password" />
        </label>
        <div data-secret>
          <label>
            <span>Inside a secret group</span>
            <textarea />
          </label>
        </div>
      </>,
    );
    watch = watchTyping(document);
    await user.type(screen.getByLabelText("Recovery code"), "7K3QD-9XM2P");
    await user.type(screen.getByLabelText("Code"), "123456");
    await user.type(screen.getByLabelText("New password"), "open-sesame-12");
    await user.type(screen.getByLabelText("Inside a secret group"), "private key");

    expect(watch.keep()).toEqual([]);
  });
});

describe("the secrets the console lets a person type in clear", () => {
  // No rule above can tell them from prose, so each carries `data-secret`.
  // A box with a "Show" toggle can be revealed BEFORE it is focused, and then
  // nothing ever saw it as a password box.
  const source = (path: string) => readFileSync(resolve(import.meta.dirname, "..", path), "utf8");

  it("include every revealable password box", () => {
    const files = sources(resolve(import.meta.dirname, ".."));
    const revealable = files.filter((file) => /\? "text" : "password"|\? "password" : "text"/.test(readFileSync(file, "utf8")));
    expect(revealable.map((file) => file.slice(file.indexOf("/src/") + 5))).toEqual(["modules/licenses/LicensesPage.tsx"]);
    const page = source("modules/licenses/LicensesPage.tsx");
    const toggle = page.indexOf('type={keyVisible ? "text" : "password"}');
    expect(page.lastIndexOf("data-secret", toggle)).toBeGreaterThan(page.lastIndexOf("<Field", toggle));
  });

  it("include the install link pasted to build an MSI: its code enrols a machine", () => {
    const panel = source("modules/agent-releases/MsiDownload.tsx");
    const box = panel.indexOf("value={link}");
    expect(panel.lastIndexOf("<input", box)).toBeGreaterThan(-1);
    expect(panel.lastIndexOf("data-secret", box)).toBeGreaterThan(panel.lastIndexOf("<input", box));
  });

  it("include the remote screen's text to type on the device (final re-review, I3)", () => {
    const viewer = source("modules/remote/ScreenViewer.tsx");
    const box = viewer.indexOf("value={text}");
    const input = viewer.lastIndexOf("<input", box);
    expect(input).toBeGreaterThan(-1);
    expect(viewer.slice(input, box)).toContain("data-secret");
    expect(viewer.slice(input, box)).toContain('autoComplete="off"');
  });

  it("include the step-up's recovery code", () => {
    const dialog = source("components/stepUp.tsx");
    const box = dialog.indexOf("value={recovery}");
    expect(dialog.lastIndexOf("<input", box)).toBeGreaterThan(-1);
    expect(dialog.lastIndexOf("data-secret", box)).toBeGreaterThan(dialog.lastIndexOf("<input", box));
  });
});

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}
