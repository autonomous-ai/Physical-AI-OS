package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// seedDevice builds a device with enrolled user dirs and one runtime's USER.md.
func seedDevice(t *testing.T, userMD string, enrolled ...string) (Options, string) {
	t.Helper()
	root := t.TempDir()

	// Empty store (fresh device) and missing store (cannot judge) must stay distinct.
	users := filepath.Join(root, "users")
	if err := os.MkdirAll(users, 0o755); err != nil {
		t.Fatalf("seed users dir: %v", err)
	}
	for _, u := range enrolled {
		if err := os.MkdirAll(filepath.Join(users, u), 0o755); err != nil {
			t.Fatalf("seed enrollment %s: %v", u, err)
		}
	}
	prev := usercanon.UsersDir
	usercanon.UsersDir = users
	t.Cleanup(func() { usercanon.UsersDir = prev })

	ws := filepath.Join(root, "openclaw", "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatalf("seed workspace: %v", err)
	}
	path := filepath.Join(ws, "USER.md")
	if err := os.WriteFile(path, []byte(userMD), 0o644); err != nil {
		t.Fatalf("seed USER.md: %v", err)
	}

	opts := Options{OpenclawWorkspace: ws}.withDefaults()
	return opts, path
}

func readFile(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read %s: %v", p, err)
	}
	return string(b)
}

// A name is stale only when its enrollment is gone.
func TestReconcileRetiresNameWithNoEnrollment(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 1 || actions[0].Kind != "name" {
		t.Fatalf("want one name retirement, got %+v", actions)
	}

	got := readFile(t, path)
	if strings.Contains(got, "**Name:** Leo") {
		t.Errorf("stale name survived:\n%s", got)
	}
	if !strings.Contains(got, "- **Name:**\n") {
		t.Errorf("the slot must remain, asking for the next answer:\n%s", got)
	}
	if !strings.Contains(got, "Update this as you go") {
		t.Errorf("template instructions must survive:\n%s", got)
	}
}

// Absence is never the trigger.
func TestReconcileNeverRetiresAnEnrolledUser(t *testing.T) {
	body := "- **Name:** Long\n- Context: Prefers Vietnamese.\n"
	opts, path := seedDevice(t, body, "long", "chloe")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 0 {
		t.Fatalf("an enrolled user must never be retired, got %+v", actions)
	}
	if readFile(t, path) != body {
		t.Errorf("file was rewritten despite nothing being stale:\n%s", readFile(t, path))
	}
}

// Display names resolve like attribution does.
func TestReconcileKeepsADisplayNameThatResolvesToAnEnrollment(t *testing.T) {
	opts, _ := seedDevice(t, "- **Name:** Long Tran\n", "long")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 0 {
		t.Fatalf("a resolvable display name must be kept, got %+v", actions)
	}
}

// A pass that finds nothing stale must not touch the file (prompt-cache miss).
func TestReconcileDoesNotWriteWhenNothingIsStale(t *testing.T) {
	opts, path := seedDevice(t, "- **Name:** Long\n", "long")

	before, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if !before.ModTime().Equal(after.ModTime()) {
		t.Error("file was rewritten with nothing stale — that is a prompt-cache miss per boot")
	}
}

func TestReconcileDryRunReportsButDoesNotWrite(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")

	actions, err := ReconcileUserProfiles(opts, false)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 1 {
		t.Fatalf("dry run must still report, got %+v", actions)
	}
	if readFile(t, path) != liveDeviceUserMD {
		t.Error("dry run modified the file")
	}
}

func TestReconcilePrunesUsersBlocksByEnrollment(t *testing.T) {
	body := "- **Name:** Long\n" +
		"- Users: **long (friend)**: prefers Vietnamese\n" +
		"- Users: **leo (friend)**: likes Billie Jean\n"
	opts, path := seedDevice(t, body, "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := readFile(t, path)
	if strings.Contains(got, "leo") {
		t.Errorf("unenrolled Users block survived:\n%s", got)
	}
	if !strings.Contains(got, "long (friend)") {
		t.Errorf("enrolled Users block was pruned:\n%s", got)
	}
}

// An empty enrollment store retires nothing.
func TestReconcileDoesNothingWhenStoreIsEmpty(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD)

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 0 {
		t.Fatalf("must no-op with an empty enrollment store, got %+v", actions)
	}
	if readFile(t, path) != liveDeviceUserMD {
		t.Error("file was modified with no enrollments to judge against")
	}
}

// "unknown" never keeps a profile alive.
func TestReconcileTreatsUnknownAsNotAPerson(t *testing.T) {
	opts, _ := seedDevice(t, "- **Name:** unknown\n", "long", "unknown")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 1 {
		t.Fatalf("`unknown` must not keep a profile alive, got %+v", actions)
	}
}

func TestReconcileErrorsRatherThanGuessWhenStoreIsMissing(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")
	usercanon.UsersDir = filepath.Join(t.TempDir(), "gone")

	if _, err := ReconcileUserProfiles(opts, true); err == nil {
		t.Error("want an error when the enrollment store is unreadable")
	}
	if readFile(t, path) != liveDeviceUserMD {
		t.Error("file must be untouched when the store cannot be read")
	}
}

func TestReconcileIsIdempotent(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("first: %v", err)
	}
	first := readFile(t, path)

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("second: %v", err)
	}
	if len(actions) != 0 {
		t.Errorf("second pass still found work: %+v", actions)
	}
	if readFile(t, path) != first {
		t.Error("second pass changed the file")
	}
}

func TestUserProfilePathsCoverEveryRuntime(t *testing.T) {
	got := UserProfilePaths(DefaultOptions("", ""))
	index := map[string]bool{}
	for _, p := range got {
		index[p] = true
	}
	for _, want := range []string{
		"/root/.openclaw/workspace/USER.md",
		"/root/.picoclaw/workspace/USER.md",
		"/root/.codex/workspace/USER.md",
		"/root/.claudecode/workspace/USER.md",
		"/root/.opencode/workspace/USER.md",
		"/root/.hermes/memories/USER.md",
	} {
		if !index[want] {
			t.Errorf("missing %q — a stale profile there is never reconciled", want)
		}
	}
}

// Retiring always leaves a backup.
func TestReconcileBacksUpBeforeRetiring(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	matches, err := filepath.Glob(path + ".bak-*")
	if err != nil || len(matches) != 1 {
		t.Fatalf("want exactly one backup beside %s, got %v (err %v)", path, matches, err)
	}
	if readFile(t, matches[0]) != liveDeviceUserMD {
		t.Errorf("backup does not hold the pre-retire contents:\n%s", readFile(t, matches[0]))
	}
}

func TestReconcileDoesNotBackUpWhenNothingIsStale(t *testing.T) {
	opts, path := seedDevice(t, "- **Name:** Long\n", "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if matches, _ := filepath.Glob(path + ".bak-*"); len(matches) != 0 {
		t.Errorf("no-op pass created backups: %v", matches)
	}
}

// Retiring a name leaves exactly one blank slot.
func TestReconcileLeavesExactlyOneBlankNameSlot(t *testing.T) {
	opts, path := seedDevice(t, liveDeviceUserMD, "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := readFile(t, path)
	if n := strings.Count(got, "- **Name:**"); n != 1 {
		t.Errorf("want exactly one Name slot, got %d:\n%s", n, got)
	}
}

// Person blocks are prunable with or without the `Users: ` prefix.
func TestReconcilePrunesUsersBlockWithoutHeadingPrefix(t *testing.T) {
	body := "- **long (friend)**: prefers Vietnamese\n- **leo (friend)**: likes Billie Jean\n"
	opts, path := seedDevice(t, body, "long")

	if _, err := ReconcileUserProfiles(opts, true); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := readFile(t, path)
	if strings.Contains(got, "leo") {
		t.Errorf("unenrolled block survived without the heading prefix:\n%s", got)
	}
	if !strings.Contains(got, "long (friend)") {
		t.Errorf("enrolled block was pruned:\n%s", got)
	}
}

// A field bullet without `(role)` is never read as a person.
func TestReconcileNeverTreatsAFieldBulletAsAPerson(t *testing.T) {
	body := "- **Name:** Long\n- **Notes:** Allergic to cilantro.\n- **What to call them:** anh Long\n"
	opts, path := seedDevice(t, body, "long")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 0 {
		t.Fatalf("field bullets must not be retired, got %+v", actions)
	}
	if readFile(t, path) != body {
		t.Errorf("file was rewritten:\n%s", readFile(t, path))
	}
}

// An oversized USER.md warns while there is still headroom.
func TestReconcileWarnsBeforeUserProfileWouldBeTruncated(t *testing.T) {
	if userProfileWarnChars >= userProfileBootstrapCap {
		t.Fatalf("warn threshold %d must leave headroom below the cap %d",
			userProfileWarnChars, userProfileBootstrapCap)
	}
	big := "- **Name:** Long\n" + strings.Repeat("- Context: padding padding padding.\n", 400)
	if len(big) <= userProfileWarnChars {
		t.Fatalf("fixture too small to cross the threshold: %d", len(big))
	}
	opts, _ := seedDevice(t, big, "long")

	actions, err := ReconcileUserProfiles(opts, true)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(actions) != 0 {
		t.Errorf("oversized file must not trigger retirements: %+v", actions)
	}
}
