package domain

import "context"

// RunExpiryErrorPrefix marks intentional OS deadlines; handlers must not recover
// them as successful partial replies or retry the cancelled work.
const RunExpiryErrorPrefix = "OS_RUN_EXPIRED: "

// RunExpirer optionally stops a specific active runtime owner after an OS
// deadline. A nil result acknowledges cancellation, not remote task completion.
// Implementations must refuse stale run IDs rather than stop a newer owner.
type RunExpirer interface {
	ExpireRun(ctx context.Context, runID, reason string) error
}
