package domain

import "context"

// RunExpiryErrorPrefix marks intentional OS deadlines; handlers must not treat them as partial success or retry.
const RunExpiryErrorPrefix = "OS_RUN_EXPIRED: "

// RunExpirer optionally stops an active runtime run after an OS deadline.
// Implementations must refuse stale run IDs rather than stop a newer owner.
type RunExpirer interface {
	ExpireRun(ctx context.Context, runID, reason string) error
}
