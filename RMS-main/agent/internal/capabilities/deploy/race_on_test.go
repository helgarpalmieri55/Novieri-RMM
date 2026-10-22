//go:build race

package deploy

// raceDetector is true when this binary was built with -race.
//
// The falsifier-2 assertions are RSS thresholds, and the race detector
// allocates shadow memory for every allocation the program makes — a cost that
// belongs to the instrumentation, not to the code the claim is about. Measured
// elsewhere in this agent (internal/capabilities/health/race_on_test.go): the
// same 1000-tick run read -16 KiB without -race and +3.95 MB with it, on the
// same machine, with nothing retained either time.
//
// So the threshold is applied only in a build that resembles the shipped
// binary. The measurement still RUNS under -race and still prints its number;
// what is withheld is the comparison, and the log line says so. The allocation
// counts, goroutine counts, fd counts and retained-heap comparisons are NOT
// conditioned on this — they are properties of the code rather than of the
// instrumentation, so they gate the build in every configuration.
const raceDetector = true
