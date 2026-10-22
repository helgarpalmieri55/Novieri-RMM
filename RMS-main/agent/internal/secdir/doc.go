// Package secdir makes a directory owner-only: 0700 on POSIX, a protected
// SYSTEM+Administrators DACL on Windows. Shared by app.deploy's scratch
// directory and agent.update's staging directory, so both use one
// implementation of the primitive that stops a local user replacing a
// verified file before it runs.
package secdir
