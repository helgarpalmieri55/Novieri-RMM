package health

import "github.com/toeshee/it-suite/agent/internal/wire"

// maxReportsPerMessage is HealthReportMsg.reports' max_length (§5.1).
//
// The bound is 64 "because a profile is capped at 32 checks, so a full summary
// plus edges fits in one message; an agent with more to say sends more messages
// rather than one unbounded one". With §0.11's cap of 32 checks a summary tick
// emits at most 32 summary lines plus at most 32 edges, which is exactly 64 --
// so under the caps this chunker never splits anything.
//
// It exists anyway, and is tested directly rather than through a policy,
// because the alternative is a message the gateway drops WHOLE:
// parse_agent_message validates `reports` and a 65-line message loses all 65
// lines with nothing but a logger.warning behind it. A bound that is only ever
// respected by arithmetic elsewhere is a bound one refactor away from being
// exceeded.
//
// NOTE, recorded because the plan asked for the opposite: plan Task 12 step 3
// asks for a test that "a 40-check policy emitting a summary plus 30 edges
// produces two messages". A 40-check policy is REFUSED -- §0.11's cap and
// HealthPolicyApplyParams' `max_length=32` both forbid it, and §0.5 falsifier 6
// requires that refusal. That test case cannot be built without breaking the
// cap the same task requires, so the split is asserted on the chunker instead.
const maxReportsPerMessage = 64

// chunk splits report lines into messages of at most n lines.
//
// It never yields an empty batch: `reports` is `min_length=1` server-side, and
// a message with an empty array is rejected by parse_agent_message with only a
// logger.warning to show for it (gateway/main.py:200-202) -- so an agent that
// sent one would look, from the endpoint, exactly like an agent that reported
// successfully.
//
// The batches are sub-slices of the input, so chunking allocates only the
// batch index. The caller owns the input; engine.take has already copied it out
// of the reusable scratch.
func chunk(lines []wire.HealthReportLine, n int) [][]wire.HealthReportLine {
	if len(lines) == 0 {
		return nil
	}
	if n < 1 {
		n = 1
	}
	out := make([][]wire.HealthReportLine, 0, (len(lines)+n-1)/n)
	for start := 0; start < len(lines); start += n {
		end := start + n
		if end > len(lines) {
			end = len(lines)
		}
		out = append(out, lines[start:end])
	}
	return out
}
