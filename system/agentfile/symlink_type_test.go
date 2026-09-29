package agentfile

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestResolveSymlinkChecksTargetType(t *testing.T) {
	root := t.TempDir()
	for _, targetName := range []string{"secret.json", "private.log", "id_rsa"} {
		t.Run(targetName, func(t *testing.T) {
			target := seedFile(t, root, targetName, 8)
			link := filepath.Join(root, targetName+".txt")
			if err := os.Symlink(target, link); err != nil {
				t.Fatal(err)
			}
			if _, _, err := Resolve(link, []string{root}); !errors.Is(err, ErrType) {
				t.Fatalf("symlink to forbidden type accepted: %v", err)
			}
		})
	}
}

func TestResolveAllowedSymlinkUsesTargetContentType(t *testing.T) {
	root := t.TempDir()
	target := seedFile(t, root, "image.PNG", 8)
	link := filepath.Join(root, "image.txt")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	_, contentType, err := Resolve(link, []string{root})
	if err != nil {
		t.Fatal(err)
	}
	if contentType != "image/png" {
		t.Fatalf("content type %q differs from resolved target", contentType)
	}
}
