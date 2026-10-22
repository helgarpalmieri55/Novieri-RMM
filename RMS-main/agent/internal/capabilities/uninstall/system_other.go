//go:build !windows && !linux && !darwin

package uninstall

import (
	"context"
	"errors"
)

// Supported: no installer exists for this OS, so there is nothing to remove
// and the capability is not advertised.
const Supported = false

var errUnsupported = errors.New("agent.uninstall is not supported on this operating system")

// LaunchHelper refuses.
func LaunchHelper(context.Context, string, string) error { return errUnsupported }

func hardenProcess() error { return nil }

type platformSystem struct{}

func (platformSystem) StopService(context.Context, string) error   { return errUnsupported }
func (platformSystem) DeleteService(context.Context, string) error { return errUnsupported }
func (platformSystem) PurgePackage(context.Context) (bool, error)  { return false, nil }
func (platformSystem) ScheduleDelete(string) error                 { return nil }
func (platformSystem) HelperExiting(context.Context)               {}
