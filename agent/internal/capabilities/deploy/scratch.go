package deploy

import (
	"fmt"
	"os"
	"path/filepath"
)

// THE SCRATCH DIRECTORY IS A CONFIDENTIALITY BOUNDARY, NOT A TEMP FOLDER.
//
// What lands in it: a script body containing whatever credentials the operator
// put in it, and an installer that is about to run as SYSTEM. Both are things
// a non-privileged local user must not be able to read, and — far more
// important — must not be able to REPLACE between the moment this agent writes
// them and the moment it executes them.
//
// That second one is the reason the directory's ACL matters more than the
// file's. A world-writable parent directory makes every file inside it
// replaceable regardless of the file's own mode, so a local user could swap
// the hash-verified installer for their own binary in the window before
// execution and have the agent run it as SYSTEM. Verifying a hash and then
// executing from a directory anyone can write is verification that proves
// nothing.
//
// So: the directory is created first, locked down, and only then written into.
// On POSIX that is mode 0700 and an owner of root. On Windows it is an
// explicit protected DACL granting SYSTEM and Administrators and nobody else
// — see perm_windows.go, which is the one piece of this package that could not
// be exercised in CI.

// scratch is one job's private directory. It is created inside the job and
// removed by Close before the handler returns.
type scratch struct {
	dir string
}

// newScratch creates the directory and applies the restrictive permissions.
//
// root is Options.ScratchRoot or the OS temp directory. The directory is
// created with MkdirTemp so the name is unpredictable — a predictable name in
// a shared temp directory is a symlink-plant waiting to happen, and on Windows
// it is a name a local user can pre-create with an ACL of their choosing.
func newScratch(root string) (*scratch, error) {
	if root == "" {
		root = os.TempDir()
	}
	dir, err := os.MkdirTemp(root, "its-deploy-")
	if err != nil {
		// The PATH is not in the message. It is this agent's own directory
		// layout and it lands in a job result.
		return nil, fmt.Errorf("could not create the job's scratch directory")
	}
	if err := restrictDir(dir); err != nil {
		_ = os.RemoveAll(dir)
		return nil, fmt.Errorf("could not restrict the job's scratch directory: %w", err)
	}
	return &scratch{dir: dir}, nil
}

// Close removes the directory and everything in it.
//
// BEST EFFORT, AND THAT IS A KNOWN GAP. A script body is written to disk for
// as long as the interpreter needs to read it, and if the removal fails —
// a file still open by a child process that outlived its parent, most likely —
// the body stays on disk until something else cleans the temp directory. The
// alternative (never writing it to disk) means -EncodedCommand or -Command,
// which is the louder EDR signal and puts the body in every process listing on
// the box. The file is the lesser exposure and the ACL is what makes it so.
func (s *scratch) Close() error {
	if s == nil || s.dir == "" {
		return nil
	}
	return os.RemoveAll(s.dir)
}

// path joins a name into the scratch directory. `name` is always one of this
// package's own constants — never a job-supplied string, so there is no
// traversal to defend against, and Base() is applied anyway because "never" is
// a property of today's callers.
func (s *scratch) path(name string) string {
	return filepath.Join(s.dir, filepath.Base(name))
}

// writeFile creates a file inside the scratch directory with owner-only
// permissions.
//
// O_EXCL, so this never writes through an existing file or a symlink somebody
// planted. Mode 0600 on POSIX; on Windows the mode is largely advisory and the
// directory's DACL is what actually restricts access, which is why restrictDir
// runs before anything is written.
func (s *scratch) writeFile(name string, data []byte) (string, error) {
	p := s.path(name)
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", fmt.Errorf("could not create the job's working file")
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return "", fmt.Errorf("could not write the job's working file")
	}
	if err := f.Close(); err != nil {
		return "", fmt.Errorf("could not write the job's working file")
	}
	return p, nil
}

// scriptFileName is the name a script body is written under. The EXTENSION is
// load-bearing on Windows: PowerShell's -File requires .ps1 and refuses
// anything else, which is a small, useful check that this agent is running
// what it thinks it is running.
func scriptFileName(interpreter string) string {
	switch interpreter {
	case "powershell":
		return "script.ps1"
	case "bash":
		return "script.sh"
	case "python":
		return "script.py"
	}
	return "script.txt"
}
