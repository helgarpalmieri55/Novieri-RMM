# Fixtures for the security-log collectors

**These documents are HAND-WRITTEN, not captured from a live machine.** No
Windows 11 endpoint and no Mac was available to this build environment (Linux
container), so every file here was written against Microsoft's published
`EventXml` rendering schema and the documented `log show --style ndjson` output
shape, with field names and values taken from the event-id definitions in
`docs/modules/security-logs/spec.md` §2.

What that means for anything asserted against them:

- The **structure** is what the parser is tested on, and it is the part most
  likely to be right: the `Event/System/EventID`, `TimeCreated@SystemTime`,
  `EventRecordID` and `EventData/Data@Name` shape is stable across Windows
  versions and is what `EvtRender(EvtRenderEventXml)` produces.
- The **field inventory** per event id is the part most likely to be
  incomplete. A real 4624 on Windows 11 carries members these files do not
  (`ImpersonationLevel`, `VirtualAccount`, `ElevatedToken`, `RestrictedAdminMode`,
  ...). The parser reads an ALLOWLIST and omits absent fields, so extra members
  are ignored and missing ones are simply not in `raw` — but a reviewer should
  not read "the tests pass" as "the `raw` document matches a real endpoint's".
- The **macOS message wording** is the weakest link, and is version-sensitive by
  nature: Apple rewords these strings between releases. See
  `docs/modules/security-logs/spec.md` §11.4.

Replacing these with real captured output from one Windows 11 box and one Mac
is the highest-value follow-up for this capability.
