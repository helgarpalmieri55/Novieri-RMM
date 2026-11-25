import { render } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { documentTitle, RecordTitleProvider, useDocumentTitle, useRecordTitle } from "./title";

describe("documentTitle (design §5: 'Página · Novieri RMM')", () => {
  it("names the page, then the product", () => {
    expect(documentTitle("Devices")).toBe("Devices · Novieri RMM");
  });

  it("names a record before its page", () => {
    expect(documentTitle("Devices", "srv-app02")).toBe("srv-app02 · Devices · Novieri RMM");
  });

  it("is the product alone when there is nothing else to say, and never says it twice", () => {
    expect(documentTitle()).toBe("Novieri RMM");
    expect(documentTitle("  ")).toBe("Novieri RMM");
    expect(documentTitle("Novieri RMM")).toBe("Novieri RMM");
  });
});

function Shell({ page, children }: { page: string; children: React.ReactNode }) {
  const [record, setRecord] = useState<string | undefined>();
  document.title = documentTitle(page, record);
  return <RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>;
}

function Record({ name }: { name: string }) {
  useRecordTitle(name);
  return null;
}

describe("useRecordTitle", () => {
  it("puts the record's name in front while the record is open, and takes it away after", () => {
    const view = render(
      <Shell page="Devices">
        <Record name="srv-app02" />
      </Shell>,
    );
    expect(document.title).toBe("srv-app02 · Devices · Novieri RMM");
    view.rerender(<Shell page="Devices">{null}</Shell>);
    expect(document.title).toBe("Devices · Novieri RMM");
  });

  it("does nothing outside the shell", () => {
    document.title = "before";
    render(<Record name="srv-app02" />);
    expect(document.title).toBe("before");
  });
});

describe("useDocumentTitle", () => {
  function Frame({ page }: { page: string }) {
    useDocumentTitle(page);
    return null;
  }

  it("names a page that has no shell, and gives the old title back", () => {
    document.title = "before";
    const view = render(<Frame page="Sign in" />);
    expect(document.title).toBe("Sign in · Novieri RMM");
    view.unmount();
    expect(document.title).toBe("before");
  });
});
