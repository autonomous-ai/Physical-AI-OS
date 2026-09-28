package schedule

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// digestVersion prefixes every Digest. It names the canonical form below, so
// the day that form has to change (a new field hashed, a different
// separator), the prefix changes with it: the backend can then tell "this
// device hashes differently" apart from "this device holds different rows",
// instead of reading every not-yet-upgraded device as drifted and forcing a
// schedule.sync on each of its info uplinks.
const digestVersion = "v1:"

// Digest fingerprints the schedule rows this device holds, for drift
// detection. The device reports it on every `info` uplink
// (MQTTInfoResponse.SchedulesDigest); the backend (stand-to-earn-worker)
// computes the same value over its own rows for this device and, when the two
// differ, forces a full schedule.sync.
//
// WHY THIS EXISTS: schedule.sync is only sent on a user edit or while a row is
// still "pending". A device that was re-set-up, factory-reset, lost its
// schedules.json, or was physically swapped under the same device record
// therefore sat with no (or stale) schedules while the backend showed every
// row "synced" — and nothing ever re-sent them. The digest is the cheap
// signal that closes that gap.
//
// The algorithm is a CROSS-REPO CONTRACT, byte for byte (see the digest spec
// shared with worker Task 19, and the golden vectors in digest_test.go):
//
//	digest    = "v1:" + lowercase_hex(sha256(canonical))
//	canonical = rows sorted by id (plain byte-wise compare), each rendered as
//	            <id>|<rev>|<requires joined by ",">, joined with "\n", no
//	            trailing newline; zero rows → the empty string.
//
// rev is base-10 (0 when unset); requires is hashed in STORED order, not
// sorted — the order the backend resolved it in, which is also the order the
// worker hashes. Only id, rev and requires take part: they are what the
// backend owns and what the drift check is about. Name, enabled, cadence and
// run bookkeeping stay out on purpose — a rev bump already covers every
// backend-side edit, and a run or pause must never read as drift.
//
// Every row counts, enabled or not; the caller passes the store's rows as the
// runner reads them (not pending local intents, which the backend does not
// hold yet). rows is not modified: the sort works on a copy.
func Digest(rows []Schedule) string {
	sorted := make([]Schedule, len(rows))
	copy(sorted, rows)
	// Go's string < is a byte-wise compare, which is exactly the spec's
	// ordering. Stable, so even (never expected) duplicate ids hash
	// deterministically for a given input.
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].ID < sorted[j].ID })

	var b strings.Builder
	for i, r := range sorted {
		if i > 0 {
			b.WriteByte('\n')
		}
		b.WriteString(r.ID)
		b.WriteByte('|')
		b.WriteString(strconv.FormatUint(r.Rev, 10))
		b.WriteByte('|')
		b.WriteString(strings.Join(r.Requires, ","))
	}
	sum := sha256.Sum256([]byte(b.String()))
	return digestVersion + hex.EncodeToString(sum[:])
}
