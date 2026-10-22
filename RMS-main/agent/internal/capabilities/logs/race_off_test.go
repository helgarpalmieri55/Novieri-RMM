//go:build !race

package logs

// raceDetector is false in an ordinary build. See race_on_test.go for why the
// RSS thresholds are conditioned on it.
const raceDetector = false
