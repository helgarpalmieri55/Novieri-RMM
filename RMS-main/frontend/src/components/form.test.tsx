/**
 * The form kernel — UX remediation cycle 3.
 *
 * THE FINDING, IN THE AUDITORS' OWN WORDS: "Submit with an empty form is a
 * silent no-op — the seventh occurrence." "'Run now' with an empty Device ID
 * is a silent no-op — the eighth occurrence." "Same silent-no-op failure
 * repeats on every form in the module." Forty-six findings, two of them
 * BLOCKERs, and one cause underneath all of them.
 *
 * THE CAUSE IS ONE EXPRESSION, repeated at more than twenty sites:
 * `<Button disabled={mutation.isPending || !someField}>`. There is no `<form>`
 * in the whole product but six; `components/ui.tsx`'s `Button` sets no `type`,
 * so every "submit" is an orphan control and Enter does nothing. **A disabled
 * button that never says why IS the silent no-op** — the user clicks, nothing
 * moves, and the interface offers no theory about which field is at fault.
 *
 * So the rule this kernel enforces, and the one every test below is really
 * about: THE SUBMIT CONTROL STAYS ENABLED when input is missing, and submitting
 * tells the user what is wrong. Disabled is reserved for "you may not do this
 * at all" (no permission, request in flight), where SaveBar already shows the
 * reason beside it.
 *
 * NOT INVENTED HERE. Two implementations in this codebase already do it right
 * and are generalised rather than replaced: `modules/settings/SaveBar.tsx`
 * (disabled with a visible reason, `role="alert"` on failure, `role="status"`
 * on success, no optimistic success anywhere) and
 * `modules/reporting/paramsForm.ts#missingRequired` (the one route in the
 * product that renders validation).
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Form, FormActions, describeSubmitError, requiredProblems } from "./form";
import { ApiError } from "../lib/api";
import { Button } from "./ui";

const user = userEvent.setup();

describe("requiredProblems", () => {
  it("names the field a human sees, not the key a developer typed", () => {
    const problems = requiredProblems(
      [{ name: "requester_id", label: "Requester" }],
      { requester_id: "" },
    );

    expect(problems).toEqual([{ name: "requester_id", message: "Requester is required" }]);
  });

  it("treats whitespace as empty, because a space is not an answer", () => {
    expect(requiredProblems([{ name: "n", label: "Name" }], { n: "   " })).toHaveLength(1);
  });

  it("carries a field's own rule, so a format error is not a second mechanism", () => {
    const problems = requiredProblems(
      [{ name: "prefix", label: "Prefix", validate: (v) => (v === "999.999.999.999/99" ? "Not a valid CIDR" : null) }],
      { prefix: "999.999.999.999/99" },
    );

    // Auditor B: "An impossible CIDR is accepted by the field and Register is
    // a silent no-op." One channel for "you cannot submit this yet", whether
    // the reason is absence or shape.
    expect(problems).toEqual([{ name: "prefix", message: "Not a valid CIDR" }]);
  });

  it("says nothing when every rule is satisfied", () => {
    expect(requiredProblems([{ name: "n", label: "Name" }], { n: "ok" })).toEqual([]);
  });
});

describe("describeSubmitError", () => {
  it("turns a bodyless 503 into a sentence — THE BLOCKER", () => {
    // Auditor A, BLOCKER: "A failed destructive API call (HTTP 503) is
    // silently swallowed — the UI shows success and the user is never told."
    // The mechanism: `ApiError(status, await response.text())`, so a bodyless
    // response carries `message === ""`, and every call site rendering
    // `error.message` renders nothing at all. An empty string is falsy, so
    // even `{error ? ... : null}` guards produced silence.
    const described = describeSubmitError(new ApiError(503, ""));

    expect(described).not.toBe("");
    expect(described.toLowerCase()).toContain("unavailable");
  });

  it("never returns an empty string, whatever it is handed", () => {
    for (const thrown of [new ApiError(500, ""), new Error(""), "", null, undefined, {}]) {
      expect(describeSubmitError(thrown).trim().length).toBeGreaterThan(0);
    }
  });

  it("passes a server's own explanation through when it wrote one", () => {
    expect(describeSubmitError(new ApiError(409, "That name is already taken"))).toBe(
      "That name is already taken",
    );
  });
});

function Harness({ onSubmit = vi.fn(), ...rest }: Record<string, unknown>) {
  const [value, setValue] = (rest.state as [string, (v: string) => void]) ?? ["", () => {}];
  return (
    <Form onSubmit={onSubmit as () => void}>
      <label htmlFor="name">Name</label>
      <input id="name" value={value} onChange={(e) => setValue(e.target.value)} />
      <FormActions
        label="Create"
        pending={Boolean(rest.pending)}
        error={rest.error}
        saved={Boolean(rest.saved)}
        problems={(rest.problems as { name: string; message: string }[]) ?? []}
      />
    </Form>
  );
}

describe("Form", () => {
  it("submits on Enter, because it is a real form", async () => {
    // "The Run job panel is not a <form>, yet its button is type='submit' —
    // Enter does not submit." Six <form> elements existed in the entire
    // product before this cycle.
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    await user.type(screen.getByLabelText("Name"), "thing{Enter}");

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does not navigate: the browser default is prevented", async () => {
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    // ON `document`, NOT ON THE FORM. React attaches its handlers at the root
    // container, so a listener on the form element itself runs BEFORE React
    // has called preventDefault and would report `false` for a form that
    // prevents it correctly. The document sees the event after the root has
    // handled it, which is the instant that answers "does the browser
    // navigate". Measured, not assumed: the form-level version of this test
    // failed against a working implementation.
    const submitted = vi.fn();
    const listener = (e: Event) => submitted(e.defaultPrevented);
    document.addEventListener("submit", listener);
    await user.click(screen.getByRole("button", { name: "Create" }));
    document.removeEventListener("submit", listener);

    expect(submitted).toHaveBeenCalledWith(true);
  });
});

describe("FormActions", () => {
  it("KEEPS THE SUBMIT CONTROL ENABLED when input is missing", () => {
    // The whole cycle in one assertion. A disabled button that never says why
    // is the silent no-op; the fix is not a better disabled state, it is
    // refusing to disable and explaining instead.
    render(<Harness problems={[{ name: "name", message: "Name is required" }]} />);

    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("names every unmet requirement, in one announced summary", () => {
    render(
      <Harness
        problems={[
          { name: "name", message: "Name is required" },
          { name: "queue", message: "Queue is required" },
        ]}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(within(alert).getByText(/Name is required/)).toBeInTheDocument();
    expect(within(alert).getByText(/Queue is required/)).toBeInTheDocument();
  });

  it("disables only while the request is in flight, and says so", () => {
    render(<Harness pending />);

    // "Create…", not "Creating…": this assertion was written against the
    // kernel's first pending label, which conjugated the verb and got it
    // wrong for every multi-word label. See the pending-label tests below.
    const button = screen.getByRole("button", { name: /Create…/ });
    expect(button).toBeDisabled();
  });

  it("announces a server failure rather than colouring it", () => {
    render(<Harness error={new ApiError(503, "")} />);

    expect(screen.getByRole("alert")).toHaveTextContent(/unavailable/i);
  });

  it("announces success only when the server said so", () => {
    render(<Harness saved />);

    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("never shows success and failure at once", () => {
    render(<Harness saved error={new ApiError(500, "boom")} />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

/**
 * Two defects the kernel itself introduced, found by the agents adopting it.
 *
 * A REAL FORM CHANGES WHAT EVERY BUTTON INSIDE IT DOES. `components/ui.tsx`'s
 * `Button` spreads its props and sets no `type`, and HTML defaults a `<button>`
 * inside a form to `type="submit"`. Before this cycle that was harmless —
 * there were six forms in the entire product, so a Button was almost never
 * inside one. Wrapping twenty forms in `<Form>` turned every Cancel button in
 * them into a second submit control: the button whose whole purpose is to
 * abandon the form now sends it.
 *
 * That is worse than the defect the cycle set out to fix, and it is the kind
 * that ships quietly: nothing throws, the form just submits when the user
 * asked it not to.
 */
describe("Button, inside a form", () => {
  it("does not submit unless it says it does", async () => {
    const onSubmit = vi.fn();
    const onCancel = vi.fn();
    render(
      <Form onSubmit={onSubmit}>
        <Button onClick={onCancel}>Cancel</Button>
        <FormActions label="Create" pending={false} />
      </Form>,
    );

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("still submits when it is the submit control", async () => {
    // The other half: defaulting to "button" must not break the control that
    // is SUPPOSED to submit. FormActions sets type="submit" explicitly.
    const onSubmit = vi.fn();
    render(
      <Form onSubmit={onSubmit}>
        <FormActions label="Create" pending={false} />
      </Form>,
    );

    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe("FormActions pending label", () => {
  it("reads as English for a multi-word label", () => {
    // The first version built the pending text as
    // `${label.replace(/e$/, "")}ing…`, which works for exactly one shape of
    // verb: "Save settings" became "Save settingsing…" and "Create profile"
    // became "Create profileing…". Two agents worked around it by shortening
    // their button labels, which is a UI copy change to dodge a bug in here.
    render(<Harness pending />);

    expect(screen.getByRole("button")).toHaveTextContent("Create…");
    expect(screen.getByRole("button")).not.toHaveTextContent("Createing");
  });

  it("takes an explicit pending label when a nicer one exists", () => {
    render(
      <Form onSubmit={vi.fn()}>
        <FormActions label="Save" pending pendingLabel="Saving…" />
      </Form>,
    );

    expect(screen.getByRole("button")).toHaveTextContent("Saving…");
  });
});

/**
 * Cycle 4: the API's error shape, translated once.
 *
 * "Raw Pydantic JSON errors confirmed in a THIRD module — this is a
 * product-wide error-handling failure, not a /docs bug" (auditor B, BLOCKER).
 * Five BLOCKERs and roughly twenty findings are one missing translation: the
 * backend has no custom validation handler, so a 422 arrives as FastAPI's
 * default `{"detail":[{"loc":…,"msg":…}]}`, falls past every status branch in
 * `describeSubmitError`, and is returned verbatim to be printed on the page.
 *
 * The rule these tests encode: a message shown to a person names the FIELD and
 * the PROBLEM. Not the type, not the location array, not the input that caused
 * it — that last one matters most, because `input` echoes what the user typed
 * and a password field's contents can end up in it.
 */
describe("describeSubmitError, against the shapes this API actually returns", () => {
  function apiError(status: number, body: unknown) {
    return new ApiError(status, typeof body === "string" ? body : JSON.stringify(body));
  }

  it("reads a plain HTTPException detail as the sentence it is", () => {
    const error = apiError(400, {
      detail: "no fields supplied; send display_name, phone, timezone and/or locale",
    });

    expect(describeSubmitError(error)).toBe(
      "no fields supplied; send display_name, phone, timezone and/or locale",
    );
  });

  it("names the field and the problem for a validation error", () => {
    const error = apiError(422, {
      detail: [
        {
          type: "string_too_long",
          loc: ["body", "title"],
          msg: "String should have at most 300 characters",
          input: "a very long title",
          ctx: { max_length: 300 },
        },
      ],
    });

    const message = describeSubmitError(error);

    expect(message).toContain("Title");
    expect(message).toContain("at most 300 characters");
    // The location array, the machine-readable type, and the value the user
    // typed are all absent: none of them help, and the last is a disclosure.
    expect(message).not.toContain("body");
    expect(message).not.toContain("string_too_long");
    expect(message).not.toContain("a very long title");
  });

  it("never echoes the rejected input, which may be a credential", () => {
    // A secret field that fails validation puts the secret in `input`. Echoing
    // it into an error banner writes it to the screen, and to any screenshot
    // of that screen.
    const error = apiError(422, {
      detail: [
        {
          type: "string_too_short",
          loc: ["body", "password"],
          msg: "String should have at least 12 characters",
          input: "hunter2-the-actual-secret",
        },
      ],
    });

    expect(describeSubmitError(error)).not.toContain("hunter2");
  });

  it("lists several problems rather than reporting only the first", () => {
    const error = apiError(422, {
      detail: [
        { type: "missing", loc: ["body", "name"], msg: "Field required" },
        { type: "missing", loc: ["body", "queue_id"], msg: "Field required" },
      ],
    });

    const message = describeSubmitError(error);

    expect(message).toContain("Name");
    expect(message).toContain("Queue");
    // Fixing one and resubmitting to discover the next is the slow way to fill
    // in a form.
  });

  it("humanises the field name rather than printing the wire name", () => {
    const error = apiError(422, {
      detail: [
        { type: "missing", loc: ["body", "default_queue_id"], msg: "Field required" },
      ],
    });

    const message = describeSubmitError(error);

    expect(message).toContain("Default queue");
    expect(message).not.toContain("default_queue_id");
  });

  it("still says something useful when the body is JSON it cannot read", () => {
    // A proxy's error page, a shape from a future version. The one thing it
    // must not do is print the payload.
    //
    // 422 AND NOT 500, and the difference is the whole test: every status with
    // a branch of its own returns before reaching the final fallback, so a 500
    // here exercised the "server failed" sentence and left the line this test
    // is named for uncovered. Measured, not assumed -- with 500 it passed
    // while the fallback still printed the body verbatim.
    const message = describeSubmitError(apiError(422, { unexpected: { nested: true } }));

    expect(message).not.toContain("{");
    expect(message).not.toContain("unexpected");
    expect(message.length).toBeGreaterThan(10);
  });
});

describe("describeSubmitError, against bodies that are not from this API", () => {
  it("does not print a proxy's HTML error page as the message", () => {
    // The settings agent found this while adopting the kernel: the "a server
    // that explained itself is believed" branch tests only for `{` and `[`, so
    // an nginx or Traefik error page — which is what a user gets when the API
    // is down and something in front of it answers instead — was rendered into
    // the UI as the explanation, tags and all.
    const html = "<html><head><title>502 Bad Gateway</title></head><body>…</body></html>";

    const message = describeSubmitError(new ApiError(502, html));

    expect(message).not.toContain("<html");
    expect(message).not.toContain("<title>");
    expect(message).toMatch(/server|unavailable/i);
  });

  it("still believes a plain sentence from our own API", () => {
    // The branch has to keep working: this is how every deliberate refusal
    // written with HTTPException reaches the user.
    const message = describeSubmitError(
      new ApiError(400, "no fields supplied; send display_name, phone, timezone and/or locale"),
    );

    expect(message).toBe("no fields supplied; send display_name, phone, timezone and/or locale");
  });
});
