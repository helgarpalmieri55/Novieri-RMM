//go:build !linux && !windows && !darwin

package health

// The fast-tick sampler on every platform that is not Linux.
//
// v1 answers nothing here, and every check reports `unavailable` (§0.7). That
// is a deliberate refusal to guess rather than an omission:
//
//   - (Superseded 2026-08-14, quoted rather than deleted so the correction is
//     legible: this bullet said `cpu.busy_pct` and `memory.used_pct` on macOS
//     come from Mach calls that "conventionally require cgo" and so "this needs
//     a real implementation, and has not had one yet". Half is now wrong and
//     half is now sharper. macOS answers `disk.free_pct`, `disk.free_bytes` and
//     `process.present` as of the macOS fast-tick samplers cycle -- see
//     sampler_darwin.go. The two Mach-backed checks are still unanswered, and
//     are now a RECORDED REFUSAL rather than a to-do: that file's header
//     carries the probe showing a hand-rolled Mach binding with NO assembly
//     trampoline at all still passes `GOOS=darwin go build`, so the binding
//     mechanism is unfalsifiable by anything this project's CI can run.)
//
//     (Superseded 2026-08-14, quoted rather than deleted so the correction is
//     legible: this bullet used to say the same of WINDOWS, and described its
//     counters as "performance counters or WMI". Both halves were wrong.
//     Windows is answered by sampler_windows.go as of the Windows fast-tick
//     samplers cycle, and it needed neither performance counters nor WMI:
//     GetSystemTimes and GlobalMemoryStatusEx are plain kernel32 calls,
//     reachable from a cgo-free binary.)
//   - `posture.antivirus_healthy` is Windows Security Center only in v1 (§4.1),
//     and "AV health" elsewhere "has no vendor-neutral answer; inventing one
//     produces a check that is confidently wrong".
//
// `unavailable` is a first-class, visible state: it is stored in
// `device_health_check_state`, surfaced in the console, and countable -- "37
// devices cannot answer the AV check" is an operational fact somebody needs
// (§0.7). It never opens an alert and never clears one, so a fleet running this
// build is not silently reported healthy.
//
// (Corrected 2026-08-14, twice in one day, and the sequence is the point. This
// sentence said "a Windows fleet" until the tag was narrowed to
// `!linux && !windows`; it was then changed to "a macOS fleet", and the tag has
// since narrowed again to `!linux && !windows && !darwin` -- sampler_darwin.go
// answers there now. Naming ANY specific platform here has been wrong twice, so
// it names none: this file is the fallback for whatever platforms remain, and
// the correct list is the build tag above rather than a sentence that has to be
// remembered.)

type hostSampler struct{}

// NewHostSampler returns the platform's fast-tick sampler.
func NewHostSampler() Sampler { return &hostSampler{} }

func (*hostSampler) Sample(string, string) Sample { return unavailable() }

// selectorIsWildcard is used by the Linux and darwin samplers, not by this
// stub; the reference keeps the helper compiled and vetted on every
// cross-compile target rather than only on the ones that happen to use it.
// (Corrected 2026-08-14: said "only by the Linux sampler" before
// sampler_darwin.go existed.)
var _ = selectorIsWildcard
