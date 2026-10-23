//go:build race

package backup

// raceDetector is true when this binary was built with -race.
//
// The RSS assertions in budget_test.go are thresholds on resident memory, and
// the race detector allocates shadow memory for every allocation the program
// makes — a cost that belongs to the instrumentation, not to the code the claim
// is about. Measured on the health engine
// (internal/capabilities/health/race_on_test.go): the same 1000-tick run read
// -16 KiB without -race and +3.95 MB with it, on the same machine, with nothing
// retained either time. A backup run allocates far more than a health tick, so
// the distortion here is larger still.
//
// So the thresholds are applied only in a build that resembles the shipped
// binary. The measurements still RUN under -race and still print their numbers;
// what is withheld is the comparison, and the log line says so. CI's own
// non-race invocation is where the assertion is live.
const raceDetector = true
