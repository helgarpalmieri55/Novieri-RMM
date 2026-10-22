//go:build !race

package deploy

// raceDetector is false in an ordinary build. See race_on_test.go for why the
// falsifier-2 RSS thresholds are conditioned on it.
const raceDetector = false
