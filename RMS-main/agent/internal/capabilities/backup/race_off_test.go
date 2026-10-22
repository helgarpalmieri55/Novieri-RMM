//go:build !race

package backup

// raceDetector is false in an ordinary build. See race_on_test.go for why the
// RSS thresholds in budget_test.go are conditioned on it.
const raceDetector = false
