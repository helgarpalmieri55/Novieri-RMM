//go:build linux

package inventory

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// Linux facts, read from /proc and /sys, and installed software from dpkg's
// status file or `rpm -qa`. No dmidecode, no cgo; rpm is the one shell-out.
//
// Every read here degrades to "" or 0 rather than failing: a container without
// /sys/class/dmi, a kernel without a serial, a distribution without
// /etc/os-release are all ordinary, and none of them is a reason for a device
// to have no inventory at all.
//
// All of it happens inside the job. Nothing here is opened, cached or refreshed
// when the agent is idle.

func gather(ctx context.Context) hostFacts {
	hostname, _ := os.Hostname()
	name, version := osRelease(readFile("/etc/os-release"))
	f := hostFacts{
		Hostname:     hostname,
		FQDN:         fqdnOf(hostname),
		OSVersion:    strings.TrimSpace(name + " " + version),
		OSBuild:      strings.TrimSpace(readFile("/proc/sys/kernel/osrelease")),
		Architecture: runtime.GOARCH,
		Serial:       dmi("product_serial"),
		CPUModel:     cpuModel(readFile("/proc/cpuinfo")),
		RAMBytes:     memTotalBytes(readFile("/proc/meminfo")),
	}
	f.Components = append(f.Components, hardwareComponents(f)...)
	f.Components = append(f.Components, blockDevices()...)
	f.Components = append(f.Components, nicComponents(newOriginProber(ctx))...)
	f.Facts = map[string]any{
		factLastBoot:     parseProcStatBtime(readFile("/proc/stat")),
		factManufacturer: dmiFact(readFile("/sys/class/dmi/id/sys_vendor")),
		factModel:        dmiFact(readFile("/sys/class/dmi/id/product_name")),
	}
	// The distribution's package database: dpkg, else rpm. apk, pacman,
	// flatpak and snap are not read; software_sources names what was, so an
	// empty or partial list is not mistaken for a complete one.
	var source string
	f.Software, source = linuxSoftware(ctx)
	if source != "" {
		f.Facts[factSoftwareSources] = []string{source}
	}
	return f
}

// dpkgStatusPath is dpkg's database of installed packages.
const dpkgStatusPath = "/var/lib/dpkg/status"

// rpmQueryFormat asks rpm for one tab-separated line per package. EPOCH
// prints "(none)" when unset; INSTALLTIME is Unix seconds.
const rpmQueryFormat = `%{NAME}\t%{EPOCH}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\t%{INSTALLTIME}\n`

// linuxSoftware reads dpkg's status file when it exists, and otherwise asks
// rpm. It returns the entries and the database they came from.
func linuxSoftware(ctx context.Context) ([]Software, string) {
	if status, err := os.Open(dpkgStatusPath); err == nil {
		defer status.Close()
		return parseDpkgStatus(status, maxSoftwareScan), "dpkg"
	}
	if out := run(ctx, "rpm", "-qa", "--queryformat", rpmQueryFormat); out != "" {
		return parseRPMQuery(out, maxSoftwareScan), "rpm"
	}
	return nil, ""
}

// hardwareComponents turns the scalar facts into the components the server
// stores individually. cpu and memory are components AND top-level fields:
// HardwareDoc carries `cpu`/`ram_bytes` for the device summary, and
// `components[]` is what the hardware view lists.
func hardwareComponents(f hostFacts) []Component {
	var out []Component
	if f.CPUModel != "" {
		out = append(out, Component{
			Type: "cpu", Identifier: f.CPUModel,
			Attributes: map[string]any{"cores": runtime.NumCPU()},
		})
	}
	if f.RAMBytes > 0 {
		out = append(out, Component{
			Type: "memory", Identifier: "system-memory",
			Attributes: map[string]any{"size_bytes": f.RAMBytes},
		})
	}
	return out
}

// blockDevices reads /sys/block. Partitions, loop, ram and device-mapper nodes
// are skipped: they are views of a disk, not disks, and each would land as a
// separate hardware component with its own history.
func blockDevices() []Component {
	entries, err := os.ReadDir("/sys/block")
	if err != nil {
		return nil
	}
	var out []Component
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") ||
			strings.HasPrefix(name, "dm-") || strings.HasPrefix(name, "zram") {
			continue
		}
		base := filepath.Join("/sys/block", name)
		attributes := map[string]any{}
		// /sys reports size in 512-byte sectors regardless of the device's own
		// block size. This is the kernel's unit, not the device's.
		if sectors, err := strconv.ParseInt(strings.TrimSpace(readFile(filepath.Join(base, "size"))), 10, 64); err == nil && sectors > 0 {
			attributes["size_bytes"] = sectors * 512
		}
		if model := strings.TrimSpace(readFile(filepath.Join(base, "device", "model"))); model != "" {
			attributes["model"] = model
		}
		if serial := strings.TrimSpace(readFile(filepath.Join(base, "device", "serial"))); serial != "" {
			attributes["serial"] = serial
		}
		out = append(out, Component{Type: "disk", Identifier: name, Attributes: attributes})
	}
	return out
}

// dmi reads a DMI field. Most of them are root-only (mode 0400), so an
// unprivileged agent gets "" -- which is why every caller treats absence as
// ordinary.
func dmi(field string) string {
	return strings.TrimSpace(readFile(filepath.Join("/sys/class/dmi/id", field)))
}

// osRelease pulls NAME and VERSION out of /etc/os-release.
//
// PRETTY_NAME is preferred where present because it is the string a human
// recognises ("Debian GNU/Linux 12 (bookworm)"); the NAME/VERSION pair is the
// fallback for distributions that omit it.
func osRelease(content string) (name, version string) {
	values := map[string]string{}
	for _, line := range strings.Split(content, "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		values[key] = strings.Trim(strings.TrimSpace(value), `"`)
	}
	if pretty := values["PRETTY_NAME"]; pretty != "" {
		return pretty, ""
	}
	return values["NAME"], values["VERSION"]
}

// cpuModel returns the first "model name" in /proc/cpuinfo.
//
// x86 uses "model name"; arm64 has no such field and uses "Model" (the board)
// or nothing at all, so an empty answer on arm is expected rather than a bug.
func cpuModel(content string) string {
	for _, line := range strings.Split(content, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		switch strings.TrimSpace(key) {
		case "model name", "Model", "Processor":
			if v := strings.TrimSpace(value); v != "" {
				return v
			}
		}
	}
	return ""
}

// memTotalBytes reads MemTotal, which /proc/meminfo reports in kB.
func memTotalBytes(content string) int64 {
	for _, line := range strings.Split(content, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if !ok || strings.TrimSpace(key) != "MemTotal" {
			continue
		}
		fields := strings.Fields(value)
		if len(fields) == 0 {
			return 0
		}
		kb, err := strconv.ParseInt(fields[0], 10, 64)
		if err != nil {
			return 0
		}
		return kb * 1024
	}
	return 0
}

// fqdnOf reports a fully-qualified name only when the host already knows one.
//
// NO DNS LOOKUP. A reverse lookup inside the job would make the collected FQDN
// a property of the network's resolver rather than of the machine, would block
// for the resolver's timeout on a device with a broken DNS server, and would
// report a stale PTR record as the device's own name.
func fqdnOf(hostname string) string {
	if strings.Contains(hostname, ".") {
		return hostname
	}
	return ""
}

// readFile is the "absence is ordinary" file read every fact above is built on.
func readFile(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(raw)
}
