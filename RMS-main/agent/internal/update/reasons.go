package update

// Stable reason tokens (spec §4.4). The server reads these; change none of
// them without changing backend/app/modules/agent_update/reasons.py too.
const (
	// Refusals.
	ReasonBadParams             = "bad_params"
	ReasonPlatformMismatch      = "platform_mismatch"
	ReasonVersionNotOrderable   = "version_not_orderable"
	ReasonDowngradeNotPermitted = "downgrade_not_permitted"
	ReasonVersionConflict       = "version_conflict"
	ReasonUpdateInProgress      = "update_in_progress"
	ReasonBusy                  = "busy"
	ReasonNotRunningAsService   = "not_running_as_service"
	ReasonUnsupportedLayout     = "unsupported_install_layout"
	ReasonBadArtifactURL        = "bad_artifact_url"
	ReasonArtifactTooLarge      = "artifact_too_large"
	ReasonInsufficientDisk      = "insufficient_disk"
	ReasonSignatureRequired     = "signature_required"
	ReasonSignatureInvalid      = "signature_invalid"
	ReasonUnbound               = "unbound"
	// Failures.
	ReasonDownloadFailed       = "download_failed"
	ReasonSizeMismatch         = "size_mismatch"
	ReasonHashMismatch         = "hash_mismatch"
	ReasonDataDirNoexec        = "data_dir_noexec"
	ReasonHelperLaunchFailed   = "helper_launch_failed"
	ReasonSelftestFailed       = "selftest_failed"
	ReasonInstallFailed        = "install_failed"
	ReasonNoConfirmation       = "no_confirmation"
	ReasonCrashLoop            = "crash_loop"
	ReasonInterrupted          = "interrupted"
	ReasonRollbackFailed       = "rollback_failed"
	ReasonStagingFailed        = "staging_failed"
	ReasonServiceControlFailed = "service_control_failed"
)

// Outcomes in AgentUpdateResult.outcome.
const (
	OutcomeUpdated        = "updated"
	OutcomeAlreadyCurrent = "already_current"
	OutcomeRolledBack     = "rolled_back"
	OutcomeAborted        = "aborted"
)
