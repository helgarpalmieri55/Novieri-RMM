import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { renderPage } from "../test/renderPage";
import { FormPage, FormSection, SaveBar, UnsavedGuard, useDraft, useUnsavedGuard } from "./formKit";

interface Org {
  name: string;
}

function Settings({ initial, onSave }: { initial: Org; onSave: (org: Org) => void }) {
  const [saved, setSaved] = useState<Org>(initial);
  const { draft, setDraft, dirty, discard } = useDraft(saved);
  return (
    <FormPage back={{ to: "/admin", label: "Admin" }} title="Settings">
      <UnsavedGuard dirty={dirty} />
      <FormSection title="Organization" icon="settings">
        <label>
          Name
          <input value={draft?.name ?? ""} onChange={(event) => setDraft({ name: event.target.value })} />
        </label>
      </FormSection>
      <SaveBar
        dirty={dirty}
        saving={false}
        onDiscard={discard}
        onSave={() => {
          if (draft) {
            onSave(draft);
            setSaved(draft);
          }
        }}
      />
    </FormPage>
  );
}

describe("SaveBar and useDraft (design §2: one save bar for the whole console)", () => {
  it("is not drawn until something changed, and Discard puts the page back", async () => {
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("saves through the page, and goes away once the server has the change", async () => {
    const save = vi.fn();
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={save} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(save).toHaveBeenCalledWith({ name: "Novieri SAS" });
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("says the save failed in the bar, and keeps the change", () => {
    renderPage(
      "/x",
      <SaveBar dirty saving={false} error={new Error("The server refused it.")} onDiscard={vi.fn()} onSave={vi.fn()} />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  // What the per-panel buttons it replaces did (settings' SaveBar, the
  // ticket's "Save changes"), so a page loses none of it by moving here.
  it("says it saved once the server answered, until the next change", () => {
    renderPage("/x", <SaveBar dirty={false} saving={false} saved onDiscard={vi.fn()} onSave={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("says why it cannot save, and does not offer to", () => {
    renderPage(
      "/x",
      <SaveBar dirty saving={false} blockedReason="A subject is required." onDiscard={vi.fn()} onSave={vi.fn()} />,
    );
    expect(screen.getByText("A subject is required.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("submits the page's form when asked, so Enter in a field still saves", async () => {
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    renderPage(
      "/x",
      <form onSubmit={onSubmit}>
        <input aria-label="Name" defaultValue="a" />
        <SaveBar dirty saving={false} submit onDiscard={vi.fn()} />
      </form>,
    );
    await userEvent.type(screen.getByLabelText("Name"), "{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

// LEAVING IS A CLICK ON A LINK, as a person leaves: the form's own back link.
// (A bare `router.navigate()` that the blocker holds leaves a pending
// transition behind, and React reports it as an un-awaited suspension.)
describe("UnsavedGuard (design §5: a page with changes warns before it is left)", () => {
  it("asks before leaving, and staying keeps the page and the change", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/settings");
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri SAS");
  });

  it("leaves when the person says so", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    expect(router.state.location.pathname).toBe("/admin");
  });

  it("lets a clean page go without asking", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(router.state.location.pathname).toBe("/admin");
  });

  it("asks the browser too, for a reload or a closed tab", async () => {
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });
});

/** An alta that leaves for what it created, the portal's New ticket shape. */
function Creating({ releasing }: { releasing: boolean }) {
  const [title, setTitle] = useState("");
  const { guard, release } = useUnsavedGuard(title !== "");
  const navigate = useNavigate();
  return (
    <FormPage back={{ to: "/tickets", label: "Tickets" }} title="New ticket">
      {guard}
      <label>
        Title
        <input value={title} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <button
        type="button"
        onClick={() => {
          if (releasing) release();
          navigate("/tickets/t-1");
        }}
      >
        Send
      </button>
    </FormPage>
  );
}

/** A form whose links change only the query: a tab, the way Settings has them. */
function Tabbed({ search }: { search?: boolean }) {
  const [name, setName] = useState("");
  return (
    <>
      <UnsavedGuard dirty={name !== ""} search={search} />
      <label>
        Name
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <Link to="/settings?tab=email">Email</Link>
    </>
  );
}

describe("UnsavedGuard, what leaving is (decision D16)", () => {
  it("lets a save that leaves go without asking", async () => {
    const { router } = renderPage("/tickets/new", <Creating releasing />, "/tickets/new");
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(router.state.location.pathname).toBe("/tickets/t-1");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("is not vacuous: the same navigation without release() asks", async () => {
    const { router } = renderPage("/tickets/new", <Creating releasing={false} />, "/tickets/new");
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/tickets/new");
  });

  it("does not call a change of query alone leaving (a tab, a filter, ?new=)", async () => {
    const { router } = renderPage("/settings", <Tabbed />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Email" }));
    expect(router.state.location.search).toBe("?tab=email");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("asks on a change of query too where the page says its query is what it edits", async () => {
    const { router } = renderPage("/settings", <Tabbed search />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Email" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });
});

describe("FormSection", () => {
  it("folds and unfolds, and says which it is", async () => {
    renderPage("/x", <FormSection title="Mail">x</FormSection>);
    const toggle = screen.getByRole("button", { name: "Mail" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("x")).not.toBeVisible();
  });
});
