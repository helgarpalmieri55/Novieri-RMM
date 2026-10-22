package capabilities

import (
	"encoding/json"
	"testing"
)

// greeting's degradation table.
//
// The end-to-end path is in integration_test.go; this is the half that can be
// driven directly, because `params` is the one thing a capability receives from
// the wire and `hello` is the one capability that reads it.
func TestGreetingNeverFailsOnParametersItDoesNotRecognise(t *testing.T) {
	for _, tc := range []struct {
		name   string
		params string
		want   string
	}{
		{"the demo's message", `{"message":"hello from the control plane"}`, "hello from the control plane"},
		{"absent message", `{}`, helloDefaultMessage},
		{"null message", `{"message":null}`, helloDefaultMessage},
		{"unknown member", `{"message":"hi","loudness":11}`, "hi"},
		{"message is not a string", `{"message":42}`, helloDefaultMessage},
		{"params is not an object", `[1,2,3]`, helloDefaultMessage},
		{"params is empty bytes", ``, helloDefaultMessage},
		{"params is null", `null`, helloDefaultMessage},
		{"unicode survives", `{"message":"héllo ☃"}`, "héllo ☃"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// FALSIFIER: the returned string. A `hello` that errored on
			// unrecognised params would report a healthy agent as broken --
			// this is the kernel's liveness job, and its failure is read as the
			// endpoint being unwell.
			if got := greeting(json.RawMessage(tc.params)); got != tc.want {
				t.Fatalf("greeting(%s) = %q; want %q", tc.params, got, tc.want)
			}
		})
	}
}
