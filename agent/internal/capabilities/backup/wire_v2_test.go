package backup

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Format 2's params and results (spec-server-backup.md §5.1, §5.3).

func v2RunParams(t *testing.T, roots []string, extra map[string]any) json.RawMessage {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(runParamsJSON(t, roots, nil), &doc); err != nil {
		t.Fatal(err)
	}
	doc["format"] = 2
	for k, v := range extra {
		doc[k] = v
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func mustRefuse(t *testing.T, err error, want string) {
	t.Helper()
	var r *jobs.Refusal
	if !errors.As(err, &r) {
		t.Fatalf("got %v (%T), want a refusal containing %q", err, err, want)
	}
	if !strings.Contains(r.Reason, want) {
		t.Fatalf("reason %q does not contain %q", r.Reason, want)
	}
}

func TestAFormat1JobReadsExactlyAsBefore(t *testing.T) {
	j, err := parseRunJob(runParamsJSON(t, []string{"/data"}, nil), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if j.format != formatV1 || j.vssType != vssTypeCopy || !j.allowLive || !j.failOnWriter || j.workloadID != nil {
		t.Fatalf("defaults = %+v", j)
	}
}

func TestAnUnknownFormatIsRefusedNotGuessedAt(t *testing.T) {
	_, err := parseRunJob(v2RunParams(t, []string{"/data"}, map[string]any{"format": 3}), time.Now())
	mustRefuse(t, err, "format 3")
	_, err = parseRestoreJob([]byte(`{"repository_id":"r","storage_ref":"s","manifest_ref":"m",
		"selection":[],"target":{},"mode":"verify","repo_key":"k","format":9}`))
	mustRefuse(t, err, "format 9")
}

func TestAJobPastItsStartDeadlineIsNotStarted(t *testing.T) {
	now := time.Date(2026, 9, 23, 3, 0, 0, 0, time.UTC)
	past := v2RunParams(t, []string{"/data"}, map[string]any{"not_after_start": "2026-09-23T02:59:00Z"})
	_, err := parseRunJob(past, now)
	mustRefuse(t, err, "start deadline")

	future := v2RunParams(t, []string{"/data"}, map[string]any{"not_after_start": "2026-09-23T03:01:00Z"})
	if _, err := parseRunJob(future, now); err != nil {
		t.Fatalf("a job inside its start deadline was refused: %v", err)
	}
	bad := v2RunParams(t, []string{"/data"}, map[string]any{"not_after_start": "tomorrow"})
	_, err = parseRunJob(bad, now)
	mustRefuse(t, err, "RFC 3339")
}

func TestTheFilesWorkloadConfigIsRead(t *testing.T) {
	raw := v2RunParams(t, []string{"/ignored"}, map[string]any{
		"workload": map[string]any{"id": "w-1", "kind": "files", "config": map[string]any{
			"paths": []string{"/srv", "/home"}, "exclude": []string{"**/*.tmp"},
			"one_filesystem": true, "allow_live": false,
		}},
		"vss": map[string]any{"backup_type": "full", "fail_on_writer": false},
	})
	j, err := parseRunJob(raw, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	roots, _ := j.Sources.roots()
	if strings.Join(roots, ",") != "/home,/srv" {
		t.Errorf("roots = %v; the workload's paths replace sources.paths", roots)
	}
	if len(j.Sources.Exclude) != 1 || !j.oneFilesystem || j.allowLive || j.vssType != vssTypeFull ||
		j.failOnWriter || j.workloadID == nil || *j.workloadID != "w-1" {
		t.Errorf("job = %+v", j)
	}
}

func TestLiveReadIsTheDefaultPerTheOperatorsDecision(t *testing.T) {
	// 2026-09-23: Linux without LVM, btrfs or ZFS gets a live backup by
	// default. A workload that says nothing about allow_live allows it.
	raw := v2RunParams(t, []string{"/srv"}, map[string]any{
		"workload": map[string]any{"id": "w", "kind": "files", "config": map[string]any{}},
	})
	j, err := parseRunJob(raw, time.Now())
	if err != nil || !j.allowLive {
		t.Fatalf("allowLive = %v, err %v", j.allowLive, err)
	}
}

func TestAWorkloadKindThisAgentDoesNotImplementIsRefused(t *testing.T) {
	raw := v2RunParams(t, []string{"/srv"}, map[string]any{
		"workload": map[string]any{"id": "w", "kind": "mssql", "config": map[string]any{}},
	})
	_, err := parseRunJob(raw, time.Now())
	mustRefuse(t, err, `"mssql" is not implemented`)
}

func TestAnUnknownVSSBackupTypeIsRefused(t *testing.T) {
	raw := v2RunParams(t, []string{"/srv"}, map[string]any{"vss": map[string]any{"backup_type": "log"}})
	_, err := parseRunJob(raw, time.Now())
	mustRefuse(t, err, "vss.backup_type")
}

func TestTheAutoSnapshotKindParses(t *testing.T) {
	var doc map[string]any
	_ = json.Unmarshal(runParamsJSON(t, []string{"/srv"}, nil), &doc)
	doc["snapshot"] = "auto"
	raw, _ := json.Marshal(doc)
	if _, err := parseRunJob(raw, time.Now()); err != nil {
		t.Fatalf("snapshot=auto refused: %v", err)
	}
}

func TestConflictModesParseAndDefaultToOverwrite(t *testing.T) {
	base := `{"repository_id":"r","storage_ref":"s","manifest_ref":"m","selection":["/a"],
		"mode":"restore","repo_key":"k","target":`
	for target, want := range map[string]string{
		`{"path":"/x"}`:                        conflictOverwrite,
		`{"path":"/x","conflict":"rename"}`:    conflictRename,
		`{"path":"/x","conflict":"skip"}`:      conflictSkip,
		`{"path":"/x","conflict":"overwrite"}`: conflictOverwrite,
	} {
		j, err := parseRestoreJob([]byte(base + target + "}"))
		if err != nil || j.conflict != want {
			t.Errorf("%s: conflict %q err %v, want %q", target, j.conflict, err, want)
		}
	}
	_, err := parseRestoreJob([]byte(base + `{"path":"/x","conflict":"merge"}}`))
	mustRefuse(t, err, "target.conflict")
	_, err = parseRestoreJob([]byte(`{"repository_id":"r","storage_ref":"s","manifest_ref":"m",
		"selection":[],"target":{"conflict":"rename"},"mode":"verify","repo_key":"k"}`))
	mustRefuse(t, err, "no conflict mode")
	_, err = parseRestoreJob([]byte(base + `{"path":"/x"},"restore_kind":"database"}`))
	mustRefuse(t, err, "restore_kind")
}

func TestAFormat2ResultCarriesTheNewMembersAndAFormat1ResultDoesNot(t *testing.T) {
	// BackupRunResult is extra="forbid" server-side: a format-1 result that
	// grew a key would fail the whole job on a server that never asked.
	ref := "manifests/x"
	id := "w-1"
	o := &runOutcome{
		res:      &runResult{State: stateSuccess, ManifestRef: &ref, DedupStats: map[string]any{}},
		cons:     &consistencyDoc{Level: levelLive, Snapshot: mechNone, Writers: []writerDoc{}, Databases: []databaseDoc{}},
		warnings: []string{warnNotSnapshotConsistent},
	}
	o.job.format = formatV1
	o.job.workloadID = &id
	v1, _ := json.Marshal(o.wire())
	for _, k := range []string{"workload_id", "consistency", "warnings"} {
		if strings.Contains(string(v1), `"`+k+`"`) {
			t.Errorf("a format-1 result carries %q: %s", k, v1)
		}
	}
	o.job.format = formatV2
	v2, _ := json.Marshal(o.wire())
	var doc map[string]any
	if err := json.Unmarshal(v2, &doc); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"state", "manifest_ref", "bytes_protected", "bytes_stored", "chunk_count",
		"dedup_stats", "error", "workload_id", "consistency", "warnings"} {
		if _, ok := doc[k]; !ok {
			t.Errorf("format-2 result lacks %q: %s", k, v2)
		}
	}
	c := doc["consistency"].(map[string]any)
	for _, k := range []string{"level", "crash_consistent", "snapshot", "writers", "files_changed_during_read",
		"non_transactional_tables", "databases"} {
		if _, ok := c[k]; !ok {
			t.Errorf("consistency lacks %q", k)
		}
	}
	if c["writers"] == nil || c["databases"] == nil {
		t.Error("consistency lists are null; they are [] when empty")
	}

	// A format-2 result with no warnings says [] and not null.
	o.warnings = nil
	v2, _ = json.Marshal(o.wire())
	if !strings.Contains(string(v2), `"warnings":[]`) {
		t.Errorf("empty warnings marshalled as %s", v2)
	}
}
