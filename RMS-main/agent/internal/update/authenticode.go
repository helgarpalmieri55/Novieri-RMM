package update

// windowsSignerPin is the expected Authenticode publisher and issuing chain
// (spec §10), set by the release workflow with -ldflags -X once Windows
// signing exists. Empty until the pilot: the check is then hash plus the
// release signature only.
var windowsSignerPin = ""

// AuthenticodePinned reports whether this build requires an Authenticode
// signature. Verification is not built yet, so a pinned build refuses every
// update rather than silently skipping the check.
func AuthenticodePinned() bool { return windowsSignerPin != "" }
