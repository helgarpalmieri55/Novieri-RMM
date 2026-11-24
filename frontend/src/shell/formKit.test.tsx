import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type FormEvent } from "react";
import { createBrowserRouter, Link, RouterProvider, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { renderPage } from "../test/renderPage";
import { FormPage, FormSection, SaveBar, UnsavedGuard, useDraft, useUnsavedGuard } from "./formKit";
import { useNewParam } from "./listKit";

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

  // A PERMISSION, never missing input (review fix round 1, item 8): input is
  // explained on submit with Save enabled (`components/form.tsx`, Rule 1).
  it("says why it cannot save, and does not offer to", () => {
    renderPage(
      "/x",
      <SaveBar
        dirty
        saving={false}
        blockedReason="You can read these settings, and an org-admin can change them."
        onDiscard={vi.fn()}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByText("You can read these settings, and an org-admin can change them.")).toBeInTheDocument();
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
function Tabbed({ search }: { search?: boolean | readonly string[] }) {
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

/** Two forms on one page, each with its own guard: the second one clean. */
function TwoGuards({ secondDirty }: { secondDirty: boolean }) {
  const [a, setA] = useState("");
  return (
    <>
      <UnsavedGuard dirty={a !== ""} />
      <input aria-label="A" value={a} onChange={(event) => setA(event.target.value)} />
      <UnsavedGuard dirty={secondDirty} />
      <Link to="/elsewhere">Leave</Link>
    </>
  );
}

describe("UnsavedGuard, one blocker per router (review fix round 1, item 2)", () => {
  it("a clean guard elsewhere on the page does not disarm a dirty one", async () => {
    const { router } = renderPage("/settings", <TwoGuards secondDirty={false} />, "/settings");
    await userEvent.type(screen.getByLabelText("A"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Leave" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it("says so loudly when two guards hold changes at once, because only one of them could ask", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      renderPage("/settings", <TwoGuards secondDirty />, "/settings");
      await userEvent.type(screen.getByLabelText("A"), "x");
      expect(error.mock.calls.map((call) => String(call[0])).join("\n")).toMatch(/two UnsavedGuards hold changes at once/);
    } finally {
      error.mockRestore();
    }
  });
});

interface Record {
  name: string;
  updated_at: number;
}

/**
 * A form whose server answers a save with the record it stored, which carries
 * a field the form never edits (`updated_at`). `Answer` delivers that answer,
 * so a test can type between the send and the answer.
 */
function Stamped({ fail = false }: { fail?: boolean }) {
  const [saved, setSaved] = useState<Record>({ name: "Novieri", updated_at: 1 });
  const [inFlight, setInFlight] = useState<Record | null>(null);
  const { draft, setDraft, dirty, markSent } = useDraft(saved);
  return (
    <>
      <UnsavedGuard dirty={dirty} />
      <input
        aria-label="Name"
        value={draft?.name ?? ""}
        onChange={(event) => draft && setDraft({ ...draft, name: event.target.value })}
      />
      <SaveBar
        dirty={dirty}
        saving={false}
        onDiscard={() => undefined}
        onSave={() => {
          if (!draft) return;
          markSent(draft);
          setInFlight(draft);
        }}
      />
      <button
        type="button"
        onClick={() => {
          if (!inFlight) return;
          if (fail) markSent(null);
          else setSaved({ name: inFlight.name, updated_at: 2 });
          setInFlight(null);
        }}
      >
        Answer
      </button>
      {/* Someone else saved meanwhile: the record changes under the form. */}
      <button type="button" onClick={() => setSaved((current) => ({ ...current, updated_at: current.updated_at + 10 }))}>
        Refetch
      </button>
      <Link to="/admin">Admin</Link>
    </>
  );
}

describe("useDraft after a save (review fix round 1, item 3)", () => {
  it("takes the server's answer as the new state, even with a field the form never edits, so leaving does not ask", async () => {
    const { router } = renderPage("/settings", <Stamped />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri SAS");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(router.state.location.pathname).toBe("/admin");
  });

  it("keeps what was typed after the send: the answer does not wipe it", async () => {
    renderPage("/settings", <Stamped />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.type(screen.getByLabelText("Name"), "!");
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri SAS!");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
  });

  it("forgets the send when the save failed, so a later refetch does not replace the unsaved change", async () => {
    renderPage("/settings", <Stamped fail />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    await userEvent.click(screen.getByRole("button", { name: "Refetch" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri SAS");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
  });
});

/** A draft inside a `?new=` dialog, guarded on the key whose change closes it. */
function DialogDraft({ search, onClose }: { search?: boolean | readonly string[]; onClose: () => void }) {
  const [title, setTitle] = useState("");
  return (
    <div role="dialog" aria-label="New ticket">
      <UnsavedGuard dirty={title !== ""} search={search} />
      <input aria-label="Title" value={title} onChange={(event) => setTitle(event.target.value)} />
      <Link to="?new=ticket&state=open">Open only</Link>
      <button type="button" onClick={onClose}>
        Close
      </button>
    </div>
  );
}

function ListWithCreate({ search }: { search?: boolean | readonly string[] }) {
  const created = useNewParam("ticket");
  return (
    <>
      <button type="button" onClick={created.start}>
        New ticket
      </button>
      {created.open ? <DialogDraft search={search} onClose={created.close} /> : null}
    </>
  );
}

describe("UnsavedGuard in a ?new= dialog (review fix round 1, item 5)", () => {
  // Close is Back here (`useNewParam` goes back from the entry it pushed); a
  // click rather than `router.navigate(-1)`, whose held promise React reports
  // as an un-awaited suspension (see "LEAVING IS A CLICK ON A LINK" above).
  it("asks before Back closes a dialog with a change in it, when the guard names the key", async () => {
    const { router } = renderPage("/service-desk", <ListWithCreate search={["new"]} />, "/service-desk");
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?new=ticket");
    expect(screen.getByLabelText("Title")).toHaveValue("Printer");
  });

  it("does not ask for a change of a key it does not name", async () => {
    const { router } = renderPage("/service-desk", <ListWithCreate search={["new"]} />, "/service-desk");
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("link", { name: "Open only" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(router.state.location.search).toBe("?new=ticket&state=open");
  });

  it("is not vacuous: without the key, Back drops the draft unasked", async () => {
    const { router } = renderPage("/service-desk", <ListWithCreate />, "/service-desk");
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await act(() => router.navigate(-1));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

/** A page that releases its guard but then does not navigate. */
function Unused() {
  const [name, setName] = useState("");
  const { guard, release } = useUnsavedGuard(name !== "");
  return (
    <>
      {guard}
      <input aria-label="Name" value={name} onChange={(event) => setName(event.target.value)} />
      <button type="button" onClick={release}>
        Release
      </button>
      <Link to="/admin">Admin</Link>
    </>
  );
}

/**
 * A `?new=` dialog that saves and closes, under the router the console runs on
 * (`createBrowserRouter`). Close after `start()` is Back, and a browser router
 * judges a POP in its `popstate` listener, a later task than the click: jsdom
 * delivers `popstate` asynchronously too, which a memory router never does.
 */
function CreateDialog({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const { guard, release } = useUnsavedGuard(title !== "", { search: ["new"] });
  return (
    <div role="dialog" aria-label="New ticket">
      {guard}
      <input aria-label="Title" value={title} onChange={(event) => setTitle(event.target.value)} />
      <button
        type="button"
        onClick={() => {
          // The server has the ticket: let the close through.
          release();
          onCreated();
        }}
      >
        Create
      </button>
    </div>
  );
}

function SaveAndClose() {
  const created = useNewParam("ticket");
  return (
    <>
      <button type="button" onClick={created.start}>
        New ticket
      </button>
      {created.open ? <CreateDialog onCreated={created.close} /> : null}
    </>
  );
}

describe("release() (review fix rounds 1 and 2, item 7 and item 1)", () => {
  it("lets a save that closes its ?new= dialog go unasked under a browser router, whose Back is judged a task later", async () => {
    window.history.replaceState(null, "", "/service-desk");
    const router = createBrowserRouter([{ path: "/service-desk", element: <SaveAndClose /> }]);
    try {
      render(<RouterProvider router={router} />);
      await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
      expect(router.state.location.search).toBe("?new=ticket");
      await userEvent.type(screen.getByLabelText("Title"), "Printer");
      await userEvent.click(screen.getByRole("button", { name: "Create" }));
      await waitFor(() => expect(router.state.location.search).toBe(""));
      expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
      expect(screen.queryByRole("dialog", { name: "New ticket" })).toBeNull();
    } finally {
      router.dispose();
    }
  });

  it("expires on the person's next click: a release no navigation used leaves the guard armed", async () => {
    const { router } = renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    await userEvent.click(screen.getByLabelText("Name"));
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it("expires on the person's next keystroke too", async () => {
    const { router } = renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    await userEvent.keyboard("y");
    await act(() => router.navigate("/admin"));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it("is not vacuous: with no act in between, the release lets the navigation through", async () => {
    const { router } = renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    await act(() => router.navigate("/admin"));
    expect(router.state.location.pathname).toBe("/admin");
  });
});

describe("the leave question (review fix round 1, item 9)", () => {
  it("draws no empty subject line", async () => {
    renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    const dialog = screen.getByRole("dialog", { name: "Leave without saving?" });
    expect(dialog.querySelector(".confirm-subject")).toBeNull();
    expect(dialog).toHaveTextContent("The changes on this page have not been saved.");
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
