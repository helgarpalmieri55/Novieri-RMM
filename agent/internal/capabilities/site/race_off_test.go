//go:build !race

package site

// raceDetector is false in an ordinary build. See race_on_test.go for why
// §5.4 falsifier 2's RSS threshold is conditioned on it.
const raceDetector = false
