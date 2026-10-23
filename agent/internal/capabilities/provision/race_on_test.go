//go:build race

package provision

// raceDetector is true when this binary was built with -race.
//
// The RSS thresholds in budget_test.go are about the shipped binary. The race
// detector allocates shadow memory for every allocation the program makes -- a
// cost that belongs to the instrumentation. Measured elsewhere in this module
// (internal/capabilities/health/race_on_test.go): the same 1000-iteration run
// read -16 KiB without -race and +3.95 MB with it, on the same machine, with
// nothing retained either time.
//
// So the measurement still RUNS under -race and still prints its number; what
// is withheld is the comparison, and the log line says so.
const raceDetector = true
