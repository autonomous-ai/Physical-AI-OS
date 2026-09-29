package bootstrap

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestUpdaterURLFrom(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		want    string
		wantErr bool
	}{
		{
			name: "production metadata url",
			in:   "https://cdn.autonomous.ai/os/ota/metadata.json",
			want: "https://cdn.autonomous.ai/os/software-update",
		},
		{
			name: "bucket-style url keeps its namespace",
			in:   "https://storage.googleapis.com/s3-autonomous-upgrade-3/os/ota/metadata.json",
			want: "https://storage.googleapis.com/s3-autonomous-upgrade-3/os/software-update",
		},
		{
			name: "query is dropped",
			in:   "https://cdn.autonomous.ai/os/ota/metadata.json?v=3",
			want: "https://cdn.autonomous.ai/os/software-update",
		},
		{
			name:    "relative url is refused",
			in:      "/os/ota/metadata.json",
			wantErr: true,
		},
		{
			name:    "no namespace to derive from",
			in:      "https://cdn.autonomous.ai/metadata.json",
			wantErr: true,
		},
		{
			name:    "empty url is refused",
			in:      "",
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := updaterURLFrom(tc.in)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected an error, got %q", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("got %q, want %q", got, tc.want)
			}
		})
	}
}

// withTempUpdaterPath swaps updaterPath for a temp destination.
func withTempUpdaterPath(t *testing.T, contents string) string {
	t.Helper()
	dir := t.TempDir()
	dest := filepath.Join(dir, "software-update")
	if err := os.WriteFile(dest, []byte(contents), 0o755); err != nil {
		t.Fatalf("seed updater: %v", err)
	}
	orig := updaterPath
	updaterPath = dest
	t.Cleanup(func() { updaterPath = orig })
	return dest
}

func TestInstallUpdaterRejectsBrokenSyntax(t *testing.T) {
	good := "#!/usr/bin/env bash\necho ok\n"
	dest := withTempUpdaterPath(t, good)

	// A truncated download (`if` without `fi`) must never reach disk.
	truncated := []byte("#!/usr/bin/env bash\nif [ -f /tmp/x ]; then\n  echo half\n")
	if err := installUpdater(context.Background(), truncated); err == nil {
		t.Fatal("expected bash -n to reject a truncated script")
	}

	after, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read updater: %v", err)
	}
	if string(after) != good {
		t.Errorf("existing updater was replaced by a broken download:\n%s", after)
	}

	entries, err := os.ReadDir(filepath.Dir(dest))
	if err != nil {
		t.Fatalf("read dir: %v", err)
	}
	if len(entries) != 1 {
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Errorf("expected only the updater to remain, got %v", names)
	}
}

func TestInstallUpdaterReplacesAndKeepsItExecutable(t *testing.T) {
	dest := withTempUpdaterPath(t, "#!/usr/bin/env bash\necho old\n")

	want := "#!/usr/bin/env bash\necho new\n"
	if err := installUpdater(context.Background(), []byte(want)); err != nil {
		t.Fatalf("install: %v", err)
	}

	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read updater: %v", err)
	}
	if string(got) != want {
		t.Errorf("got %q, want %q", got, want)
	}

	info, err := os.Stat(dest)
	if err != nil {
		t.Fatalf("stat updater: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o755 {
		t.Errorf("mode = %o, want 755", perm)
	}
}

func TestUpdateInFlightBlocksRefresh(t *testing.T) {
	if updateInFlight() {
		t.Fatal("nothing should be in flight at the start")
	}
	inFlight.Store("hal", struct{}{})
	defer inFlight.Delete("hal")
	if !updateInFlight() {
		t.Error("a running force update must report as in flight")
	}
}
