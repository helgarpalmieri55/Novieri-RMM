//go:build race

package patch

// raceDetector is true when this binary was built with -race.
//
// Falsifier 2 is an RSS threshold, and the race detector allocates shadow
// memory for every allocation the program makes -- a cost that belongs to the
// instrumentation, not to the code the claim is about. Measured on the health
// engine (internal/capabilities/health/race_on_test.go): the same 1000-tick run
// read -16 KiB without -race and +3.95 MB with it, on the same machine, with
// nothing retained either time.
//
// So the threshold is applied only in a build that resembles the shipped
// binary. The measurement still RUNS under -race and still prints its number;
// what is withheld is the COMPARISON, and the log line says so. CI's ordinary
// invocation carries no -race, so the assertion is live where it matters rather
// than quietly retired.
const raceDetector = true
