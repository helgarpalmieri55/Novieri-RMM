//go:build !race

package health

// raceDetector is false in an ordinary build. See race_on_test.go for why
// §0.5 falsifier 1's RSS threshold is conditioned on it.
const raceDetector = false
