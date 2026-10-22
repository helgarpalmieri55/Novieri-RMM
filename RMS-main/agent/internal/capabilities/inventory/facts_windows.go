//go:build windows

package inventory

import (
	"context"
	"errors"
	"io"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/windows/registry"
)

// Windows facts: ONE PowerShell invocation for the CIM facts and address
// origins, in-process registry reads for installed software and Entra join,
// and ONE more PowerShell invocation for Store/MSIX packages, run alongside
// the first. All of it inside the job, with compile-time-constant scripts.
// Everything that interprets the output is in winfacts.go, winsoftware.go and
// origin.go, which have no build tag and are tested on linux -- so what is
// unexercised there is the exec calls and the registry reads.
//
// No WMI COM bindings and no `wmic`: COM would mean cgo (this binary is
// CGO_ENABLED=0 on all six targets) and `wmic` is deprecated and absent from
// current Windows builds.

// factsTimeout bounds the facts PowerShell: a wedged WMI repository must not
// hold the collection until collectTimeout.
const factsTimeout = 20 * time.Second

// appxTimeout bounds the Store package listing. Get-AppxPackage -AllUsers is
// the slowest query here, commonly several seconds and on a busy machine many
// more; it runs concurrently with the facts, so it costs the collection its
// own duration rather than adding to theirs.
const appxTimeout = 15 * time.Second

// uninstallKeyPath is the Uninstall key, relative to HKLM (in both registry
// views) and to each HKU\<sid>.
const uninstallKeyPath = `SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`

func gather(ctx context.Context) hostFacts {
	hostname, _ := os.Hostname()

	// Started first, read last. The channel is buffered and run() returns by
	// appxTimeout plus its WaitDelay, and gather always receives, so neither
	// the goroutine nor the process outlives the job.
	appxCtx, cancelAppx := context.WithTimeout(ctx, appxTimeout)
	defer cancelAppx()
	appx := make(chan string, 1)
	go func() {
		appx <- run(appxCtx, "powershell.exe",
			"-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
			"-Command", windowsAppxScript)
	}()

	factsCtx, cancelFacts := context.WithTimeout(ctx, factsTimeout)
	raw := run(factsCtx, "powershell.exe",
		"-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
		"-Command", windowsFactsScript)
	cancelFacts()
	f := parseWindowsFacts(raw)

	f.Hostname = hostname
	f.Architecture = runtime.GOARCH
	if f.FQDN == "" && strings.Contains(hostname, ".") {
		f.FQDN = hostname
	}
	if f.Facts == nil {
		f.Facts = map[string]any{}
	}
	if joined, ok := entraJoined(); ok {
		f.Facts[factEntraJoined] = joined
	}
	f.Components = append(f.Components, nicComponents(windowsOriginsFrom(raw))...)

	var used []string
	if sw := registrySoftware(); len(sw) > 0 {
		f.Software = append(f.Software, sw...)
		used = append(used, "registry")
	}
	if sw := parseAppx(<-appx); len(sw) > 0 {
		f.Software = append(f.Software, sw...)
		used = append(used, "appx")
	}
	f.Facts[factSoftwareSources] = used
	return f
}

// registrySoftware reads the Uninstall key in HKLM's 64- and 32-bit views and
// in every loaded user hive under HKU.
func registrySoftware() []Software {
	var entries []uninstallEntry
	entries = readUninstall(entries, registry.LOCAL_MACHINE, uninstallKeyPath, registry.WOW64_64KEY)
	entries = readUninstall(entries, registry.LOCAL_MACHINE, uninstallKeyPath, registry.WOW64_32KEY)
	if users, err := registry.OpenKey(registry.USERS, "", registry.ENUMERATE_SUB_KEYS); err == nil {
		sids, _ := users.ReadSubKeyNames(-1)
		users.Close()
		for _, sid := range sids {
			if isUserHive(sid) {
				entries = readUninstall(entries, registry.USERS, sid+`\`+uninstallKeyPath, 0)
			}
		}
	}
	return softwareFromUninstall(entries)
}

func readUninstall(entries []uninstallEntry, root registry.Key, path string, view uint32) []uninstallEntry {
	parent, err := registry.OpenKey(root, path, registry.ENUMERATE_SUB_KEYS|view)
	if err != nil {
		return entries
	}
	defer parent.Close()
	names, _ := parent.ReadSubKeyNames(-1)
	for _, name := range names {
		if len(entries) >= maxSoftwareScan {
			break
		}
		k, err := registry.OpenKey(parent, name, registry.QUERY_VALUE|view)
		if err != nil {
			continue
		}
		entries = append(entries, uninstallEntry{
			DisplayName:     regString(k, "DisplayName"),
			DisplayVersion:  regString(k, "DisplayVersion"),
			Publisher:       regString(k, "Publisher"),
			InstallDate:     regString(k, "InstallDate"),
			ParentKeyName:   regString(k, "ParentKeyName"),
			ReleaseType:     regString(k, "ReleaseType"),
			SystemComponent: regFlag(k, "SystemComponent"),
		})
		k.Close()
	}
	return entries
}

// regString reads a REG_SZ or REG_EXPAND_SZ (unexpanded), or "".
func regString(k registry.Key, name string) string {
	v, _, err := k.GetStringValue(name)
	if err != nil {
		return ""
	}
	return v
}

// regFlag reads a DWORD flag. Some installers write it as a string.
func regFlag(k registry.Key, name string) bool {
	if v, _, err := k.GetIntegerValue(name); err == nil {
		return v == 1
	}
	n, err := strconv.Atoi(strings.TrimSpace(regString(k, name)))
	return err == nil && n == 1
}

// entraJoined reports Entra ID (Azure AD) join, including hybrid join: the
// JoinInfo key has one subkey per join certificate. Absent key means not
// joined; any other error means unknown.
func entraJoined() (joined, ok bool) {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE,
		`SYSTEM\CurrentControlSet\Control\CloudDomainJoin\JoinInfo`, registry.ENUMERATE_SUB_KEYS)
	if errors.Is(err, registry.ErrNotExist) {
		return false, true
	}
	if err != nil {
		return false, false
	}
	defer k.Close()
	names, err := k.ReadSubKeyNames(1)
	if len(names) > 0 {
		return true, true
	}
	// ReadSubKeyNames(1) reports io.EOF for an empty key.
	if err == nil || errors.Is(err, io.EOF) {
		return false, true
	}
	return false, false
}
