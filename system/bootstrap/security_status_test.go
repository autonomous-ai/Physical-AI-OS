package bootstrap

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/bootstrap/config"
)

func TestSecurityStatusReportsLegacyWithoutKey(t *testing.T) {
	b := &Bootstrap{cfg: &config.Config{}}

	status := b.securityStatus()
	if status.Mode != SecurityModeLegacy {
		t.Fatalf("mode = %q, want %q", status.Mode, SecurityModeLegacy)
	}
	if status.ArtifactChecksums {
		t.Fatal("artifact checksums cannot be enforced without a pinned key")
	}
	if status.KeyFingerprint != "" {
		t.Fatalf("unexpected fingerprint %q in legacy mode", status.KeyFingerprint)
	}
	if status.LastMetadataFetch != nil {
		t.Fatal("no fetch has happened yet")
	}
}

func TestSecurityStatusReportsVerifiedWithKey(t *testing.T) {
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	encoded := base64.StdEncoding.EncodeToString(publicKey)
	b := &Bootstrap{cfg: &config.Config{SigningPublicKey: "  " + encoded + "\n"}}

	status := b.securityStatus()
	if status.Mode != SecurityModeVerified {
		t.Fatalf("mode = %q, want %q", status.Mode, SecurityModeVerified)
	}
	if !status.ArtifactChecksums {
		t.Fatal("verified mode must report artifact checksum enforcement")
	}
	if want := publicKeyFingerprint(encoded); status.KeyFingerprint != want {
		t.Fatalf("fingerprint = %q, want %q", status.KeyFingerprint, want)
	}
	if len(status.KeyFingerprint) != 16 {
		t.Fatalf("fingerprint %q is not 16 characters", status.KeyFingerprint)
	}
}

func TestSecurityStatusRecordsLastFetchOutcome(t *testing.T) {
	b := &Bootstrap{cfg: &config.Config{}}

	b.security.record(true, nil)
	last := b.securityStatus().LastMetadataFetch
	if last == nil || !last.Verified || last.Error != "" {
		t.Fatalf("unexpected success record: %+v", last)
	}

	// A later failure must replace the earlier success.
	b.security.record(false, errors.New("fetch metadata: status 404"))
	last = b.securityStatus().LastMetadataFetch
	if last == nil || last.Verified {
		t.Fatalf("failure was not recorded: %+v", last)
	}
	if last.Error != "fetch metadata: status 404" {
		t.Fatalf("error = %q", last.Error)
	}
}

// Signed-only feeds carry no top-level entries; verification must still succeed.
func TestVerifyOTAMetadataAcceptsSignedOnlyDocument(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"os-server":{"version":"1.2.3"}}`)
	envelope := `{"signed":{"format":"` + otaMetadataFormat + `","payload":"` +
		base64.StdEncoding.EncodeToString(payload) + `","signature":{"algorithm":"ed25519","value":"` +
		base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload)) + `"}}}`

	got, err := verifyOTAMetadata([]byte(envelope), base64.StdEncoding.EncodeToString(publicKey))
	if err != nil {
		t.Fatalf("verify signed-only metadata: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("payload = %s", got)
	}
}
