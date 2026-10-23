package jobs

import "errors"

// ErrDeferred means the capability has handed the job to another process that
// will report the terminal result through the persisted outbox later. The
// runner sends nothing beyond `running`.
var ErrDeferred = errors.New("job result deferred to a later process")

// DeferrableJobType is the only job type allowed to return ErrDeferred. The
// process that stages a self-update is not the process that learns whether it
// worked; for any other type a deferral would be a job that never answers.
const DeferrableJobType = "agent.update"

// Executing reports how many jobs are running now, not counting excludeID.
func (r *Runner) Executing(excludeID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := len(r.running)
	if _, ok := r.running[excludeID]; ok {
		n--
	}
	return n
}
