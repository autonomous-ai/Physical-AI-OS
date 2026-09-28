package schedule

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// digestVersion prefixes every Digest; bump it whenever the canonical form changes.
const digestVersion = "v1:"

// Digest fingerprints the stored rows for backend drift detection (info uplink).
// Cross-repo contract, byte for byte (golden vectors in digest_test.go):
// "v1:" + hex(sha256(rows sorted by id as "<id>|<rev>|<requires joined by ,>" joined by "\n")).
// rows is not modified.
func Digest(rows []Schedule) string {
	sorted := make([]Schedule, len(rows))
	copy(sorted, rows)
	// Byte-wise, stable sort as the spec requires.
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
