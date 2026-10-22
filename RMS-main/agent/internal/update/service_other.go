//go:build !linux && !darwin && !windows

package update

import (
	"context"
	"errors"
)

// SystemServices refuses everything on a platform the agent does not ship to.
type SystemServices struct{}

var errNoServiceManager = errors.New("no supported service manager on this platform")

func (SystemServices) Identify(context.Context) (string, error) {
	return "", fail(ReasonNotRunningAsService, errNoServiceManager)
}

func (SystemServices) LaunchHelper(context.Context, string, string, Layout, string) error {
	return errNoServiceManager
}

func (SystemServices) StopForSwap() bool             { return false }
func (SystemServices) Stop(context.Context) error    { return errNoServiceManager }
func (SystemServices) Start(context.Context) error   { return errNoServiceManager }
func (SystemServices) Restart(context.Context) error { return errNoServiceManager }
func (SystemServices) HelperExiting(context.Context) {}
