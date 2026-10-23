package patch

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// Homebrew cask updates on macOS. No build tag: parsing runs in CI on Linux;
// the engine is selected only on darwin (engine_darwin.go).
//
// brew refuses to run as root, so it runs as the account that owns the
// Homebrew prefix, with that account's home and a fixed environment
// (brew_darwin.go). That is the account that installed the casks, and running
// its own tool as it is no privilege this agent does not already have. A
// prefix owned by root, or no brew at all, is "unsupported", with the reason.
//
// Only casks (applications), and only ones brew itself calls outdated: casks
// that update themselves (`auto_updates`) or track `latest` are left to their
// own updater, which is brew's default without --greedy.
//
// NEVER EXECUTED: no Mac has run this. The JSON shape is `brew outdated
// --cask --json=v2` as documented.

// brewRunner runs brew as the prefix owner. env is complete, not appended to
// the agent's.
type brewRunner func(ctx context.Context, args ...string) commandResult

// caskToken is what brew accepts as a cask name.
var caskToken = regexp.MustCompile(`^[a-z0-9][a-z0-9@._+-]{0,127}$`)

type brewEngine struct {
	run     brewRunner
	updated bool
}

func (b *brewEngine) Source() string      { return sourceHomebrew }
func (b *brewEngine) RestartNeeded() bool { return false }

// Outdated refreshes brew's index once, then lists outdated casks. A failed
// refresh is not fatal: the list is then as fresh as the last update.
func (b *brewEngine) Outdated(ctx context.Context) ([]entry, error) {
	if !b.updated {
		b.updated = true
		_ = b.run(ctx, "update", "--quiet")
	}
	res := b.run(ctx, "outdated", "--cask", "--json=v2")
	if !res.ok() {
		return nil, fmt.Errorf("brew outdated failed (exit %d): %s", res.ExitCode, firstLine(res.Stderr))
	}
	return parseBrewOutdated(res.Stdout)
}

// Upgrade runs `brew upgrade --cask <token>` for a token brew itself listed.
func (b *brewEngine) Upgrade(ctx context.Context, e entry) error {
	res := b.run(ctx, "upgrade", "--cask", e.Identifier)
	if res.ok() {
		return nil
	}
	return fmt.Errorf("brew upgrade failed (exit %d): %s", res.ExitCode,
		firstLine(res.Stderr+"\n"+res.Stdout))
}

type brewOutdated struct {
	Casks []struct {
		Name              string   `json:"name"`
		InstalledVersions []string `json:"installed_versions"`
		CurrentVersion    string   `json:"current_version"`
	} `json:"casks"`
}

func parseBrewOutdated(stdout string) ([]entry, error) {
	var doc brewOutdated
	if err := json.Unmarshal([]byte(strings.TrimSpace(stdout)), &doc); err != nil {
		return nil, fmt.Errorf("brew outdated did not print its JSON: %w", err)
	}
	out := make([]entry, 0, len(doc.Casks))
	for _, c := range doc.Casks {
		if !caskToken.MatchString(c.Name) {
			continue
		}
		installed := strings.Join(c.InstalledVersions, ", ")
		out = append(out, entry{
			Source:     sourceHomebrew,
			Identifier: c.Name,
			Title:      fmt.Sprintf("%s %s → %s", c.Name, installed, c.CurrentVersion),
			// Homebrew publishes no severity.
			Severity: "unknown",
		})
	}
	return out, nil
}
