package plugin

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPluginNamesRejectPathAndUnitInjection(t *testing.T) {
	for _, name := range []string{"", "../x", "/tmp/x", "x/y", ".", "..", "a\nUser=root", "a b"} {
		t.Run(name, func(t *testing.T) {
			if validatePluginName(name) == nil {
				t.Fatal("invalid name accepted")
			}

			if writeSystemdUnit(name, "/unused", "main.py") == nil {
				t.Fatal("invalid unit name accepted")
			}
		})
	}
	for _, name := range []string{"my-plugin", "plugin_123", strings.Repeat("a", 64)} {
		if err := validatePluginName(name); err != nil {
			t.Fatalf("valid name %q rejected: %v", name, err)
		}
	}
}

func TestPluginInstallRejectsOptionURLBeforeClone(t *testing.T) {
	for _, url := range []string{"--upload-pack=evil", " -cfoo=bar", "-repository"} {
		if _, err := ProvideService().Install(url); err == nil || !strings.Contains(err.Error(), "must not start") {
			t.Fatalf("option URL %q: %v", url, err)
		}
	}
}

func TestPluginInstallRejectsManifestTraversal(t *testing.T) {
	// Fake only git; invalid manifest must stop before mv, venv or systemd.
	bin := t.TempDir()
	script := `#!/bin/sh
if [ "$1" != clone ] || [ "$2" != --depth=1 ] || [ "$3" != -- ]; then
 exit 42
fi
printf '%s' '{"name":"../x"}' > "$5/plugin.json"
`
	if err := os.WriteFile(filepath.Join(bin, "git"), []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin)
	_, err := ProvideService().Install("https://example.invalid/plugin.git")
	if err == nil || !strings.Contains(err.Error(), "plugin.json: invalid plugin name") {
		t.Fatalf("manifest traversal not rejected: %v", err)
	}
}

func TestExistingPluginNamesPreserveSafeLegacyNames(t *testing.T) {
	for _, name := range []string{"Legacy.Plugin", "old_plugin"} {
		if err := validatePluginName(name); err != nil {
			t.Fatalf("legacy name rejected: %v", err)
		}
	}
	for _, name := range []string{"", ".", "..", "../x", "x/y", "a b", "*", "x\nUser=root"} {
		if validatePluginName(name) == nil {
			t.Fatalf("unsafe lifecycle name %q accepted", name)
		}
	}
}
