//go:build !windows

package install

import (
	"fmt"
	"os"
)

// Run is Windows-only. A binary renamed to the installer's name on macOS or
// Linux says where to look instead of doing anything.
func Run(_ []string, _ string, _ Link) int {
	fmt.Fprintln(os.Stderr, "The ITMS self-installer runs on Windows. On macOS and Linux, "+
		"run the command shown on the install link page.")
	return 2
}
