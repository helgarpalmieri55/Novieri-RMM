package inventory

import (
	"encoding/json"
	"strconv"
	"strings"
	"time"
)

// The Windows fact PARSER, deliberately in a file with no build tag.
//
// The file is NOT called parse_windows.go: a `_windows.go` suffix is an
// IMPLICIT build constraint in Go, so that name would have hidden this parser
// from every non-Windows build and from this container's test run -- the exact
// opposite of the point. Measured, not recalled: the rename came from
// `undefined: parseWindowsFacts` on linux.
//
// facts_windows.go holds the shell-out and nothing else; everything that
// interprets what came back lives here, so it compiles and is tested on the
// linux container this repository is built in. What remains unexercised on
// Windows is one exec.Command call -- not the mapping, not the JSON shape, not
// the unit conversions. That is the difference between "written but untested"
// and "written, with the untested part named".

// windowsFactsScript is the one PowerShell command for the host's facts: CIM,
// BitLocker, boot time, disks and the address origins, all in one process,
// because starting Windows PowerShell costs more than any one query in it.
//
// It is a compile-time constant with no interpolation: rule 1 in exec.go. The
// CIM classes are the documented ones, and `-Compress` keeps the output a
// single line so a partial read is a JSON error rather than a plausible-looking
// truncation.
const windowsFactsScript = `$ErrorActionPreference='SilentlyContinue';` +
	`$os=Get-CimInstance Win32_OperatingSystem;` +
	`$cs=Get-CimInstance Win32_ComputerSystem;` +
	`$bios=Get-CimInstance Win32_BIOS;` +
	`$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1;` +
	// ProtectionStatus of the system drive: 0 off, 1 on, 2 unknown. The
	// namespace is absent on Home editions, which leaves $bl null.
	`$bl=Get-CimInstance -Namespace root/cimv2/Security/MicrosoftVolumeEncryption -ClassName Win32_EncryptableVolume | ` +
	`Where-Object DriveLetter -eq $env:SystemDrive | Select-Object -First 1;` +
	// Rendered as ISO 8601 here: Windows PowerShell's ConvertTo-Json writes a
	// DateTime as "\/Date(...)\/". Guarded so a missing $os cannot end the
	// statement that builds the whole object.
	`$boot=$null;if($os){$boot=$os.LastBootUpTime.ToUniversalTime().ToString('o')};` +
	`[pscustomobject]@{` +
	`caption=$os.Caption;version=$os.Version;build=$os.BuildNumber;` +
	`domain=$cs.Domain;dnshost=$cs.DNSHostName;memory=[string]$cs.TotalPhysicalMemory;` +
	`manufacturer=$cs.Manufacturer;model=$cs.Model;partofdomain=$cs.PartOfDomain;user=$cs.UserName;` +
	`boot=$boot;bitlocker=$bl.ProtectionStatus;` +
	`serial=$bios.SerialNumber;cpu=$cpu.Name;cores=$cpu.NumberOfCores;` +
	`disks=@(Get-CimInstance Win32_DiskDrive | ForEach-Object { [pscustomobject]@{` +
	`id=$_.DeviceID;model=$_.Model;serial=$_.SerialNumber;size=[string]$_.Size } });` +
	`origins=@(` + windowsOriginRows + `)` +
	`} | ConvertTo-Json -Compress -Depth 4`

// windowsFacts is windowsFactsScript's output.
//
// Every numeric field is a STRING. TotalPhysicalMemory and Size are UInt64 and
// exceed float64's exact-integer range on a large disk; decoding them into
// `any` would silently reformat them, which is exactly the class of bug
// internal/canonical exists to prevent on the wire. The script casts them to
// strings and this parses them as int64.
type windowsFacts struct {
	Caption string `json:"caption"`
	Version string `json:"version"`
	Build   string `json:"build"`
	Domain  string `json:"domain"`
	DNSHost string `json:"dnshost"`
	Memory  string `json:"memory"`
	Serial  string `json:"serial"`
	CPU     string `json:"cpu"`
	Cores   int    `json:"cores"`
	// Extra facts. Pointers where "unknown" differs from false/0.
	Manufacturer string `json:"manufacturer"`
	Model        string `json:"model"`
	PartOfDomain *bool  `json:"partofdomain"`
	User         string `json:"user"`
	Boot         string `json:"boot"`
	BitLocker    *int   `json:"bitlocker"`
	Disks        []struct {
		ID     string `json:"id"`
		Model  string `json:"model"`
		Serial string `json:"serial"`
		Size   string `json:"size"`
	} `json:"disks"`
	// `origins` is read separately, by windowsOriginsFrom.
}

// windowsOriginsFrom reads the address origins out of windowsFactsScript's
// output. Anything unparseable is an empty index: every address `unknown`.
func windowsOriginsFrom(raw string) *windowsOrigins {
	var parsed struct {
		Origins json.RawMessage `json:"origins"`
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(raw)), &parsed); err != nil {
		return &windowsOrigins{}
	}
	return parseWindowsOrigins(string(parsed.Origins))
}

// parseWindowsFacts maps the script's output onto hostFacts.
//
// Unparseable output yields zero facts rather than an error: the caller has
// already got a hostname and an architecture from the Go runtime, and a
// PowerShell that failed is not a reason for the device to have no inventory.
func parseWindowsFacts(raw string) hostFacts {
	var parsed windowsFacts
	if err := json.Unmarshal([]byte(strings.TrimSpace(raw)), &parsed); err != nil {
		return hostFacts{}
	}
	f := hostFacts{
		OSVersion: strings.TrimSpace(parsed.Caption + " " + parsed.Version),
		OSBuild:   strings.TrimSpace(parsed.Build),
		Serial:    strings.TrimSpace(parsed.Serial),
		CPUModel:  strings.TrimSpace(parsed.CPU),
		RAMBytes:  parseInt64(parsed.Memory),
		Facts: map[string]any{
			factManufacturer: parsed.Manufacturer,
			factModel:        parsed.Model,
			factConsoleUser:  parsed.User,
			factLastBoot:     isoTimestamp(parsed.Boot),
		},
	}
	if parsed.PartOfDomain != nil {
		f.Facts[factDomainJoined] = *parsed.PartOfDomain
	}
	if parsed.BitLocker != nil {
		switch *parsed.BitLocker {
		case 0:
			f.Facts[factDiskEncryption] = false
		case 1:
			f.Facts[factDiskEncryption] = true
		}
	}
	if host := strings.TrimSpace(parsed.DNSHost); host != "" {
		if domain := strings.TrimSpace(parsed.Domain); domain != "" && !strings.EqualFold(domain, "WORKGROUP") {
			f.FQDN = host + "." + domain
		}
	}
	if f.CPUModel != "" {
		attributes := map[string]any{}
		if parsed.Cores > 0 {
			attributes["cores"] = parsed.Cores
		}
		f.Components = append(f.Components, Component{
			Type: "cpu", Identifier: f.CPUModel, Attributes: attributes,
		})
	}
	if f.RAMBytes > 0 {
		f.Components = append(f.Components, Component{
			Type: "memory", Identifier: "system-memory",
			Attributes: map[string]any{"size_bytes": f.RAMBytes},
		})
	}
	for _, disk := range parsed.Disks {
		identifier := strings.TrimSpace(disk.ID)
		if identifier == "" {
			continue
		}
		attributes := map[string]any{}
		if size := parseInt64(disk.Size); size > 0 {
			attributes["size_bytes"] = size
		}
		if model := strings.TrimSpace(disk.Model); model != "" {
			attributes["model"] = model
		}
		if serial := strings.TrimSpace(disk.Serial); serial != "" {
			attributes["serial"] = serial
		}
		f.Components = append(f.Components, Component{
			Type: "disk", Identifier: identifier, Attributes: attributes,
		})
	}
	return f
}

func parseInt64(s string) int64 {
	v, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	if err != nil || v < 0 {
		return 0
	}
	return v
}

// isoTimestamp normalises an ISO 8601 / RFC 3339 time to RFC 3339 UTC, or ""
// when it is not one.
func isoTimestamp(raw string) string {
	t, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}
