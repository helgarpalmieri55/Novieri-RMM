package patch

import (
	"context"
)

// Third-party application updates, alongside the OS engine.
//
// An app source (winget on Windows, Homebrew casks on macOS) lists what is
// outdated and upgrades one package at a time. Its entries travel in the same
// `patches` list as OS updates, with `source` naming the manager, so the same
// approval and ring model governs them: only a package ID an operator approved
// for the device's ring is ever sent back in `patch.apply`, and even then it is
// only a lookup key into what the manager itself just listed.
//
// A source that cannot run here is reported "unsupported" with the reason; one
// that fails is reported "failed". Neither costs the OS scan.

const (
	sourceWinget   = "winget"
	sourceHomebrew = "homebrew"
)

// appSourceNames are the `source` values app engines produce. An older control
// plane is sent none of them.
var appSourceNames = map[string]bool{sourceWinget: true, sourceHomebrew: true}

// appEngine is one package manager's outdated list and upgrade.
type appEngine interface {
	Source() string
	Outdated(ctx context.Context) ([]entry, error)
	// Upgrade installs the newest version of an entry Outdated produced.
	Upgrade(ctx context.Context, e entry) error
	// RestartNeeded reports whether an upgrade this job ran asked for one.
	RestartNeeded() bool
}

// appSlot is a source this OS has a place for: an engine, or the reason there
// is none.
type appSlot struct {
	source      string
	eng         appEngine
	unsupported string
}

// appSource implements PatchScanDocument's $defs/AppSourceStatus.
type appSource struct {
	Source string  `json:"source"`
	State  string  `json:"state"`
	Detail *string `json:"detail"`
}

// withApps is the OS engine plus its app sources. Everything not overridden is
// the OS engine's.
type withApps struct {
	engine
	apps   []appSlot
	status []appSource
}

func newWithApps(primary engine, apps ...appSlot) engine {
	if len(apps) == 0 {
		return primary
	}
	return &withApps{engine: primary, apps: apps}
}

func (w *withApps) Available(ctx context.Context) ([]entry, error) {
	out, err := w.engine.Available(ctx)
	if err != nil {
		return nil, err
	}
	w.status = w.status[:0]
	for _, a := range w.apps {
		if a.eng == nil {
			detail := a.unsupported
			w.status = append(w.status, appSource{Source: a.source, State: "unsupported", Detail: &detail})
			continue
		}
		list, err := a.eng.Outdated(ctx)
		if err != nil {
			detail := sanitizeError(err.Error())
			w.status = append(w.status, appSource{Source: a.source, State: "failed", Detail: &detail})
			continue
		}
		out = append(out, list...)
		w.status = append(w.status, appSource{Source: a.source, State: "scanned"})
	}
	return out, nil
}

func (w *withApps) Install(ctx context.Context, e entry) error {
	for _, a := range w.apps {
		if a.eng != nil && e.Source == a.eng.Source() {
			return a.eng.Upgrade(ctx, e)
		}
	}
	return w.engine.Install(ctx, e)
}

func (w *withApps) RebootRequired(ctx context.Context) bool {
	for _, a := range w.apps {
		if a.eng != nil && a.eng.RestartNeeded() {
			return true
		}
	}
	return w.engine.RebootRequired(ctx)
}

// rebootBlind reports whether the OS half cannot tell a pending restart
// outside an install.
func rebootBlind(eng engine) bool {
	if w, ok := eng.(*withApps); ok {
		eng = w.engine
	}
	_, blind := eng.(rebootBlindAtScan)
	return blind
}

// appSourcesOf is what the scan reports about app sources; nil when this OS
// has none.
func appSourcesOf(eng engine) []appSource {
	if w, ok := eng.(*withApps); ok {
		return append([]appSource{}, w.status...)
	}
	return nil
}
