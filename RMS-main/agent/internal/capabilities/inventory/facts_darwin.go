//go:build darwin

package inventory

import (
	"context"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// macOS facts: sw_vers, sysctl, ioreg, stat and fdesetup, each a fixed argv,
// each inside the job; installed software from the /Applications bundles
// (macsoftware.go). The parsers are in macfacts.go, macsoftware.go and
// plist.go, which have no build tag and are tested on linux; what is
// unexercised here is the exec calls.
//
// system_profiler is deliberately NOT used for the scalar facts: it takes
// seconds to answer and its XML output is far larger than the four values
// needed. Disks are not enumerated -- doing it properly means diskutil's plist,
// and a partial disk inventory reads as a complete one.

func gather(ctx context.Context) hostFacts {
	hostname, _ := os.Hostname()
	name, version, build := parseSWVers(run(ctx, "sw_vers"))

	f := hostFacts{
		Hostname:     hostname,
		Architecture: runtime.GOARCH,
		OSVersion:    strings.TrimSpace(name + " " + version),
		OSBuild:      build,
		Serial:       parseIORegSerial(run(ctx, "ioreg", "-rd1", "-c", "IOPlatformExpertDevice")),
		CPUModel:     strings.TrimSpace(run(ctx, "sysctl", "-n", "machdep.cpu.brand_string")),
	}
	if strings.Contains(hostname, ".") {
		f.FQDN = hostname
	}
	if memsize, err := strconv.ParseInt(strings.TrimSpace(run(ctx, "sysctl", "-n", "hw.memsize")), 10, 64); err == nil {
		f.RAMBytes = memsize
	}
	if f.CPUModel != "" {
		f.Components = append(f.Components, Component{
			Type: "cpu", Identifier: f.CPUModel,
			Attributes: map[string]any{"cores": runtime.NumCPU()},
		})
	}
	if f.RAMBytes > 0 {
		f.Components = append(f.Components, Component{
			Type: "memory", Identifier: "system-memory",
			Attributes: map[string]any{"size_bytes": f.RAMBytes},
		})
	}
	f.Components = append(f.Components, nicComponents(newOriginProber(ctx))...)

	f.Facts = map[string]any{
		factManufacturer:   "Apple Inc.",
		factModel:          strings.TrimSpace(run(ctx, "sysctl", "-n", "hw.model")),
		factLastBoot:       parseKernBoottime(run(ctx, "sysctl", "-n", "kern.boottime")),
		factConsoleUser:    macConsoleUser(run(ctx, "stat", "-f", "%Su", "/dev/console")),
		factDiskEncryption: parseFDESetup(run(ctx, "fdesetup", "status")),
	}
	f.Software = macApplications(os.DirFS("/"), maxSoftwareScan)
	if len(f.Software) > 0 {
		f.Facts[factSoftwareSources] = []string{"applications"}
	}
	return f
}
