import { act, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState, type FormEvent } from "react";
import { createBrowserRouter, createMemoryRouter, Link, RouterProvider, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Dialog } from "../components/ui";
import { renderPage } from "../test/renderPage";
import { FormPage, FormSection, SaveBar, UnsavedGuard, useDraft, useUnsavedGuard } from "./formKit";
import { useNewParam } from "./listKit";
// Kit requests batch 1 through `components.tsx`, the kit's one door.
import { FormSection as FormSectionFromKit, useEditParam } from "./components";

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

  // Critique answer 2 (.impeccable/critica-respuestas.md): the bench draws the
  // resting, failing and blocked bars side by side, so each needs its own name
  // or axe's landmark-unique fails. A page draws one bar and names none.
  it("takes a region name, so more than one bar can be drawn and told apart", () => {
    renderPage(
      "/x",
      <>
        <SaveBar dirty saving={false} onDiscard={vi.fn()} onSave={vi.fn()} />
        <SaveBar dirty saving={false} label="Unsaved changes, the save failed" error={new Error("x")} onDiscard={vi.fn()} onSave={vi.fn()} />
      </>,
    );
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Unsaved changes, the save failed" })).toHaveTextContent("Unsaved changes");
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

/**
 * THE SAME PAIRING FOR AN EDITOR (batch 1 review, F4): `useEditParam` with
 * `UnsavedGuard search={["edit"]}`, as `useEditParam`'s doc prescribes, pinned
 * as the `?new=` dialog's is above.
 */
function EditDraft({ search, onClose }: { search?: boolean | readonly string[]; onClose: () => void }) {
  const [name, setName] = useState("Pilot");
  return (
    <div role="dialog" aria-label="Edit policy">
      <UnsavedGuard dirty={name !== "Pilot"} search={search} />
      <input aria-label="Name" value={name} onChange={(event) => setName(event.target.value)} />
      <Link to="?edit=pol-1&state=open">Open only</Link>
      <button type="button" onClick={onClose}>
        Close
      </button>
    </div>
  );
}

function ListWithEdit({ search }: { search?: boolean | readonly string[] }) {
  const edit = useEditParam("policy");
  return (
    <>
      <button type="button" onClick={() => edit.start("pol-1")}>
        Edit Pilot
      </button>
      {edit.id !== "" ? <EditDraft search={search} onClose={edit.close} /> : null}
    </>
  );
}

describe("UnsavedGuard in a ?edit= editor (batch 1 review, F4)", () => {
  it("asks before Close drops an edit with a change in it, when the guard names the key, and keeps the draft", async () => {
    const { router } = renderPage("/backup/policies", <ListWithEdit search={["edit"]} />, "/backup/policies");
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.type(screen.getByLabelText("Name"), " ring");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?edit=pol-1");
    expect(screen.getByLabelText("Name")).toHaveValue("Pilot ring");
  });

  it("does not ask for a change of a key it does not name, a filter inside the editor", async () => {
    const { router } = renderPage("/backup/policies", <ListWithEdit search={["edit"]} />, "/backup/policies");
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.type(screen.getByLabelText("Name"), " ring");
    await userEvent.click(screen.getByRole("link", { name: "Open only" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(router.state.location.search).toBe("?edit=pol-1&state=open");
    expect(screen.getByLabelText("Name")).toHaveValue("Pilot ring");
  });

  it("is not vacuous: without the key, Close drops the draft unasked", async () => {
    const { router } = renderPage("/backup/policies", <ListWithEdit />, "/backup/policies");
    await userEvent.click(screen.getByRole("button", { name: "Edit Pilot" }));
    await userEvent.type(screen.getByLabelText("Name"), " ring");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Edit policy" })).toBeNull();
    expect(router.state.location.search).toBe("");
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

  // A misused release (`release(); await save(); navigate()` with a save that
  // failed) must not leave a dirty form disarmed for the toolbar's Back, a
  // reload or a navigation in code: a backstop expires it after 2 s. A real
  // Back reaches the guard in milliseconds.
  //
  // Release is pressed with `focus()` and `click()`, which fire no pointerdown
  // or keydown, so only the clock can expire it. (Focus on the button, as a
  // person's click leaves it: a held navigation with focus still in the text
  // field makes jsdom's React report an un-awaited suspension.)
  async function pressReleaseWithoutAnAct() {
    await act(async () => {
      const button = screen.getByRole("button", { name: "Release" });
      button.focus();
      button.click();
    });
  }

  it("expires after 2 s with no act and no navigation, so a later navigation in code asks again", async () => {
    const { router } = renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await pressReleaseWithoutAnAct();
      vi.advanceTimersByTime(2000);
    } finally {
      vi.useRealTimers();
    }
    await act(() => router.navigate("/admin"));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
  });

  it("is not vacuous: just under 2 s the release still lets the navigation through", async () => {
    const { router } = renderPage("/settings", <Unused />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await pressReleaseWithoutAnAct();
      vi.advanceTimersByTime(1900);
    } finally {
      vi.useRealTimers();
    }
    await act(() => router.navigate("/admin"));
    expect(router.state.location.pathname).toBe("/admin");
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

describe("FormSection's description (kit requests batch 1, item 3)", () => {
  const LEAD = "Queues are where tickets wait for someone to pick them up.";

  it("leads the section's body with it, before the fields", () => {
    renderPage(
      "/service-desk/admin",
      <FormSectionFromKit title="Queues" description={LEAD}>
        <label>
          Name
          <input />
        </label>
      </FormSectionFromKit>,
    );
    const toggle = screen.getByRole("button", { name: "Queues" });
    const body = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    const lead = screen.getByText(LEAD);
    expect(lead.tagName).toBe("P");
    expect(lead).toHaveClass("note", "form-section-lead");
    expect(body?.firstElementChild).toBe(lead);
    // It is not the button's name: the section is still called by its title.
    expect(toggle).toHaveAccessibleName("Queues");
  });

  it("folds with the body it leads", async () => {
    renderPage("/x", <FormSectionFromKit title="Queues" description={LEAD}>x</FormSectionFromKit>);
    await userEvent.click(screen.getByRole("button", { name: "Queues" }));
    expect(screen.getByText(LEAD)).not.toBeVisible();
  });

  it("draws no empty paragraph without one", () => {
    const { container } = renderPage("/x", <FormSectionFromKit title="Mail">x</FormSectionFromKit>);
    expect(container.querySelector(".form-section-lead")).toBeNull();
  });

  it("is on /__preview, so the browser's a11y pass measures it", () => {
    const preview = readFileSync(resolve(import.meta.dirname, "../preview/PreviewPage.tsx"), "utf8");
    const at = preview.search(/<FormSection\s[^>]*\bdescription=/);
    expect(at).toBeGreaterThan(-1);
  });
});

/**
 * THE LEAVE QUESTION OVER A FORM'S DIALOG (kit requests batch 2, G1 and G2).
 * Found in Chromium by a wave's review: the question and a form's `Dialog`
 * shared `.modal`'s layer, so a page that rendered its guard before the
 * dialog had the form painted over the question, and Escape reached both.
 */
function DialogForm({ guardInside = false, onClose }: { guardInside?: boolean; onClose?: () => void }) {
  const created = useNewParam("ticket");
  const [title, setTitle] = useState("");
  const { guard } = useUnsavedGuard(title !== "", { search: ["new"] });
  const close = () => {
    onClose?.();
    created.close();
  };
  return (
    <>
      <button type="button" onClick={created.start}>
        New ticket
      </button>
      {guardInside ? null : guard}
      <Dialog open={created.open} onClose={close} title="New ticket">
        {guardInside ? guard : null}
        <label>
          Title
          <input value={title} onChange={(event) => setTitle(event.target.value)} />
        </label>
        <button type="button" onClick={close}>
          Cancel
        </button>
      </Dialog>
    </>
  );
}

async function askToLeave(guardInside = false, onClose?: () => void) {
  const view = renderPage("/service-desk", <DialogForm guardInside={guardInside} onClose={onClose} />, "/service-desk");
  await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
  await userEvent.type(screen.getByLabelText("Title"), "Printer");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  return {
    ...view,
    form: screen.getByRole("dialog", { name: "New ticket" }),
    question: screen.getByRole("dialog", { name: "Leave without saving?" }),
  };
}

describe("the leave question over a form's dialog (kit requests batch 2, G1)", () => {
  it("is drawn after the form's dialog, in a layer of its own, when the page renders the guard first", async () => {
    const { form, question } = await askToLeave();
    // Later in the document AND in its own layer: on top whatever the page's order.
    expect(form.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const layer = question.closest(".leave-layer");
    expect(layer).not.toBeNull();
    expect(layer?.parentElement).toBe(document.body);
    // Its scrim is in the layer too, so the form behind is dimmed and cannot be clicked.
    expect(layer?.querySelector(".overlay.open")).not.toBeNull();
  });

  it("is drawn outside the form's dialog when the guard is inside it, so the panel cannot position or clip it", async () => {
    const { form, question } = await askToLeave(true);
    expect(form.contains(question)).toBe(false);
    expect(question.closest(".leave-layer")?.parentElement).toBe(document.body);
  });

  it("takes focus, and gives it back to the form when the person stays", async () => {
    const { form, question } = await askToLeave();
    expect(question).toContainElement(document.activeElement as HTMLElement);
    await userEvent.click(within(question).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(form).toContainElement(document.activeElement as HTMLElement);
    expect(screen.getByLabelText("Title")).toHaveValue("Printer");
  });

  it("still leaves when the person says so", async () => {
    const { router } = await askToLeave();
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    expect(screen.queryByRole("dialog", { name: "New ticket" })).toBeNull();
    expect(router.state.location.search).toBe("");
  });
});

/**
 * UNDER STRICTMODE, AS THE DEV SERVER RUNS THE CONSOLE (batch 2 review, H1).
 * `main.tsx` wraps the app in `<StrictMode>`, which runs mount effects twice:
 * a question mounted already open recorded itself as its own opener on the
 * second run, and "stay" dropped focus on <body>. Every browser gate runs on
 * the dev server, so it is tested the way they see it.
 */
async function askToLeaveStrict(guardInside: boolean) {
  const router = createMemoryRouter(
    [
      { path: "/service-desk", element: <DialogForm guardInside={guardInside} /> },
      { path: "*", element: <p>elsewhere</p> },
    ],
    { initialEntries: ["/service-desk"] },
  );
  render(
    <StrictMode>
      <RouterProvider router={router} />
    </StrictMode>,
  );
  await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
  await userEvent.type(screen.getByLabelText("Title"), "Printer");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  return {
    form: screen.getByRole("dialog", { name: "New ticket" }),
    question: screen.getByRole("dialog", { name: "Leave without saving?" }),
  };
}

describe("the leave question gives focus back to the form under StrictMode (batch 2 review, H1)", () => {
  for (const guardInside of [false, true]) {
    const where = guardInside ? "inside the dialog" : "before the dialog";
    it(`after "stay", with the guard ${where}`, async () => {
      const { form, question } = await askToLeaveStrict(guardInside);
      await userEvent.click(within(question).getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
      expect(form).toContainElement(document.activeElement as HTMLElement);
      expect(screen.getByLabelText("Title")).toHaveValue("Printer");
    });

    it(`after Escape, with the guard ${where}`, async () => {
      const { form } = await askToLeaveStrict(guardInside);
      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
      expect(form).toContainElement(document.activeElement as HTMLElement);
    });
  }
});

describe("Escape on the leave question (kit requests batch 2, G2)", () => {
  it("answers the question alone: it stays, and the form stays open with what was typed", async () => {
    const closes = vi.fn();
    const { router } = await askToLeave(false, closes);
    expect(closes).toHaveBeenCalledTimes(1);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Printer");
    expect(router.state.location.search).toBe("?new=ticket");
    // The form's dialog never heard it: it did not try to close a second time.
    expect(closes).toHaveBeenCalledTimes(1);
  });

  it("answers it alone when the guard is inside the dialog too", async () => {
    const closes = vi.fn();
    await askToLeave(true, closes);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(screen.getByLabelText("Title")).toHaveValue("Printer");
    expect(closes).toHaveBeenCalledTimes(1);
  });

  it("is not held back once the question is gone: the next Escape is the form's again", async () => {
    const closes = vi.fn();
    await askToLeave(false, closes);
    await userEvent.keyboard("{Escape}");
    await userEvent.keyboard("{Escape}");
    // The form tried to close, so it asks again: the guard still holds the change.
    expect(closes).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
  });

  it("is not vacuous: with nothing typed, Escape closes the form's dialog as before", async () => {
    const { router } = renderPage("/service-desk", <DialogForm />, "/service-desk");
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "New ticket" })).toBeNull();
    expect(router.state.location.search).toBe("");
  });
});
