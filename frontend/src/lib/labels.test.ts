/**
 * The enum-to-label kernel.
 *
 * Every case below is a value one of the two audits quoted from the live
 * product, so this file doubles as the record of which findings it closes.
 */

import { describe, expect, it } from "vitest";

import { formatBytes, formatCount } from "./format";
import { humaniseEnum, humaniseKey } from "./labels";

describe("humaniseEnum", () => {
  it("renders the values the audits caught reaching the screen raw", () => {
    expect(humaniseEnum("not_applicable")).toBe("Not applicable"); // patch tab badge
    expect(humaniseEnum("refused_loop")).toBe("Refused loop"); // disposition filter
    expect(humaniseEnum("ssh_password")).toBe("SSH password"); // credential type
    expect(humaniseEnum("high")).toBe("High"); // portal priority
    expect(humaniseEnum("hard_bounce")).toBe("Hard bounce"); // delivery failure
  });

  it("keeps initialisms as initialisms", () => {
    // "Sla breach" is worse than the raw value: it reads as a spelling
    // mistake rather than as an untranslated identifier.
    expect(humaniseEnum("sla_breach")).toBe("SLA breach");
    expect(humaniseEnum("ok")).toBe("OK");
    expect(humaniseEnum("cpu_high")).toBe("CPU high");
    expect(humaniseEnum("mdm_enrolled")).toBe("MDM enrolled");
    expect(humaniseEnum("mcp_tool_called")).toBe("MCP tool called"); // audit action
    // A BARE TWO-LETTER FRAGMENT IS THE FAILURE MODE THIS MAP EXISTS FOR.
    // /audit humanised its `ai_action` target type as "Ai action"; the same
    // fragment appears in ai-admin and ai-automation, and every one of them
    // wants "AI". `rls` is the same shape: /platform-status carried a local
    // label table for one row because `humaniseKey("rls")` said "Rls".
    expect(humaniseEnum("ai_action")).toBe("AI action");
    expect(humaniseEnum("rls_enabled")).toBe("RLS enabled");
  });

  it("uses sentence case, not title case", () => {
    // These sit inside badges beside ordinary prose; title case in that
    // position reads as a proper noun.
    expect(humaniseEnum("awaiting_customer_reply")).toBe("Awaiting customer reply");
  });

  it("splits camelCase as well as snake_case", () => {
    expect(humaniseEnum("inProgress")).toBe("In progress");
    expect(humaniseEnum("patch.scan")).toBe("Patch scan");
    expect(humaniseEnum("not-applicable")).toBe("Not applicable");
  });

  it("leaves prose alone, so applying it defensively is safe", () => {
    expect(humaniseEnum("Awaiting approval")).toBe("Awaiting approval");
    expect(humaniseEnum("")).toBe("");
  });
});

describe("humaniseKey", () => {
  it("drops the trailing _id, which names the storage not the thing", () => {
    expect(humaniseKey("default_queue_id")).toBe("Default queue");
    expect(humaniseKey("size_bytes")).toBe("Size bytes");
  });

  it("does not empty a key that is only an id", () => {
    expect(humaniseKey("id")).toBe("ID");
  });
});

describe("formatBytes", () => {
  it("formats the two counts the audit quoted from one page", () => {
    // The hardware table printed `{"size_bytes":256022937600}` verbatim while
    // the Memory card beside it read "15.0 GiB".
    expect(formatBytes(256022937600)).toBe("238 GiB");
    expect(formatBytes(16093573120)).toBe("15.0 GiB");
  });

  it("scales through the binary units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
    expect(formatBytes(5 * 1024 ** 4)).toBe("5.0 TiB");
  });

  it("does not offer a fraction of a byte", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1)).toBe("1 B");
  });

  it("returns empty for a non-number rather than printing NaN", () => {
    expect(formatBytes(Number.NaN)).toBe("");
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe("");
  });
});

describe("formatCount", () => {
  it("separates thousands", () => {
    expect(formatCount(1234567)).toBe((1234567).toLocaleString());
  });
});
