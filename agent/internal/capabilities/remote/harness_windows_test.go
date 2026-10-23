//go:build windows

package remote

import "github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"

// realHelper is the production helper entry point, parent check included:
// the parent is this same test binary, as the service is in production.
func realHelper() int { return helper.Main() }
