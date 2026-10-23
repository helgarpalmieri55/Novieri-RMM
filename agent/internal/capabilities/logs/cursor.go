package logs

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// THE ONLY THING THIS CAPABILITY REMEMBERS BETWEEN JOBS.
//
// It is ~200 bytes, it is written only during a verified job, and it is read
// only at the start of one. That is the whole of this capability's footprint at
// rest, and budget_test.go measures it rather than describing it.
//
// # Why there is a cursor at all
//
// Without one, every `logs.ship` re-reads the security log from the beginning:
// hundreds of megabytes on a machine that has been up a year, every time the
// control plane's `ship_interval_s` fires. The server would dedupe it all away
// on `(device_id, event_uid)` and nothing would be visibly wrong -- while the
// endpoint burned its idle budget reading the same events forever. The cursor
// is what makes at-least-once delivery cheap instead of quadratic.
//
// # Why the position is an opaque string
//
// Windows has a per-channel monotonic `EventRecordID`; macOS's unified log has
// no record id and is queried by time. A typed cursor would have to be a union
// of the two, and the union would be interpreted by whichever source ran --
// which is exactly the mistake `Source` records guards against. So the position
// is opaque, and the SOURCE NAME travels with it: a cursor written by another
// source is discarded rather than misread, because a Windows record id handed
// to the macOS source as a `--start` timestamp collects nothing and reports
// success.

// cursorVersion is the on-disk shape. A future change that cannot be read by an
// older agent bumps this, and an unrecognised version is treated as no cursor
// at all -- collection restarts and the server dedupes, which is the only
// failure mode this file is allowed to have.
const cursorVersion = 1

// cursorFile lives beside identity.json, nonces.json and outbound.log in the
// agent's data directory (config.DirMode 0700).
const cursorFile = "security-logs-cursor.json"

// rfc3339 matches app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
const rfc3339 = "2006-01-02T15:04:05Z"

// Cursor is the persisted read position.
type Cursor struct {
	V        int    `json:"v"`
	Source   string `json:"source"`
	Position string `json:"position"`
	// UpdatedAt is for a human reading the state directory during an incident.
	// Nothing reads it back.
	UpdatedAt string `json:"updated_at"`
}

// ErrNoCursor is not returned to callers; a missing file is an empty Cursor and
// no error, because "this agent has never shipped" is an ordinary state and not
// a failure.
var errUnknownVersion = errors.New("cursor: unrecognised on-disk version")

// fileCursorStore is the production CursorStore.
type fileCursorStore struct{ path string }

// defaultCursorStore resolves the state-directory path from the SAME source
// main did -- internal/config, i.e. the environment this process was started
// with -- at JOB TIME, not at registration. That is what keeps registration a
// map insert and the data directory untouched while the agent is idle.
func defaultCursorStore() (CursorStore, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, err
	}
	return fileCursorStore{path: filepath.Join(cfg.DataDir, cursorFile)}, nil
}

func (f fileCursorStore) Load() (Cursor, error) {
	raw, err := os.ReadFile(f.path)
	if err != nil {
		if os.IsNotExist(err) {
			// Never shipped. Not an error.
			return Cursor{}, nil
		}
		return Cursor{}, err
	}
	var c Cursor
	if err := json.Unmarshal(raw, &c); err != nil {
		return Cursor{}, fmt.Errorf("%s: %w", f.path, err)
	}
	if c.V != cursorVersion {
		return Cursor{}, fmt.Errorf("%s: %w: %d", f.path, errUnknownVersion, c.V)
	}
	return c, nil
}

// Save writes the cursor atomically: a temp file in the same directory, then a
// rename.
//
// Atomic because the alternative is a truncated JSON document after a power cut
// mid-write, which Load then reports as corrupt and ship() answers by
// restarting collection from the beginning of the retained log. That is
// survivable, and it is a lot of re-reading to accept for the sake of two fewer
// syscalls on a path that runs once per job.
//
// 0600, inside the 0700 data directory. The cursor is not a secret -- it is a
// record id -- but it is a statement about what this agent has already shipped,
// and an unprivileged local process that could rewrite it could make the agent
// skip an arbitrary range of the Security log. That is a gap in the evidence
// trail chosen by whoever wrote the file.
func (f fileCursorStore) Save(c Cursor) error {
	c.V = cursorVersion
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	dir := filepath.Dir(f.path)
	tmp, err := os.CreateTemp(dir, cursorFile+".*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name) // no-op once the rename has happened
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, f.path)
}

// sortStrings is sort.Strings, wrapped so logs.go does not import sort for one
// call on a path that must stay cheap.
func sortStrings(s []string) { sort.Strings(s) }
