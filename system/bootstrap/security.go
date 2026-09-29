package bootstrap

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

const otaMetadataFormat = "autonomous-ota/v1"

var sha256HexRe = regexp.MustCompile(`^[a-f0-9]{64}$`)

type signedOTAMetadata struct {
	Format    string `json:"format"`
	Payload   string `json:"payload"`
	Signature struct {
		Algorithm string `json:"algorithm"`
		Value     string `json:"value"`
	} `json:"signature"`
}

// verifyOTAMetadata verifies the Ed25519 signature over the exact decoded payload bytes.
func verifyOTAMetadata(data []byte, encodedPublicKey string) ([]byte, error) {
	publicKey, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encodedPublicKey))
	if err != nil {
		return nil, fmt.Errorf("decode signing public key: %w", err)
	}
	if len(publicKey) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("signing public key is %d bytes, want %d", len(publicKey), ed25519.PublicKeySize)
	}

	// Compatibility feeds keep legacy top-level entries and put the signed document under "signed".
	var wrapper struct {
		Signed json.RawMessage `json:"signed"`
	}
	if err := json.Unmarshal(data, &wrapper); err != nil {
		return nil, fmt.Errorf("decode metadata wrapper: %w", err)
	}
	if len(wrapper.Signed) != 0 && string(wrapper.Signed) != "null" {
		data = wrapper.Signed
	}

	var envelope signedOTAMetadata
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, fmt.Errorf("decode signed metadata envelope: %w", err)
	}
	if envelope.Format != otaMetadataFormat {
		return nil, fmt.Errorf("unsupported metadata format %q", envelope.Format)
	}
	if envelope.Signature.Algorithm != "ed25519" {
		return nil, fmt.Errorf("unsupported metadata signature algorithm %q", envelope.Signature.Algorithm)
	}
	payload, err := base64.StdEncoding.DecodeString(envelope.Payload)
	if err != nil {
		return nil, fmt.Errorf("decode metadata payload: %w", err)
	}
	signature, err := base64.StdEncoding.DecodeString(envelope.Signature.Value)
	if err != nil {
		return nil, fmt.Errorf("decode metadata signature: %w", err)
	}
	if !ed25519.Verify(ed25519.PublicKey(publicKey), payload, signature) {
		return nil, fmt.Errorf("metadata signature verification failed")
	}
	return payload, nil
}

func decodeOTAMetadata(data []byte, encodedPublicKey string) (domain.OTAMetadata, error) {
	payload, err := verifyOTAMetadata(data, encodedPublicKey)
	if err != nil {
		return nil, err
	}
	var metadata domain.OTAMetadata
	if err := json.Unmarshal(payload, &metadata); err != nil {
		return nil, fmt.Errorf("decode verified metadata payload: %w", err)
	}
	if err := validateOTAMetadata(metadata); err != nil {
		return nil, err
	}
	return metadata, nil
}

func validateOTAMetadata(metadata domain.OTAMetadata) error {
	for key, component := range metadata {
		if strings.TrimSpace(component.URL) == "" {
			continue
		}
		checksum := strings.ToLower(strings.TrimSpace(component.SHA256))
		if !sha256HexRe.MatchString(checksum) {
			return fmt.Errorf("metadata[%s].sha256 must be a SHA-256 hex digest when url is set", key)
		}
	}
	return nil
}

func verifyArtifactSHA256(data []byte, expected string) error {
	expected = strings.ToLower(strings.TrimSpace(expected))
	if !sha256HexRe.MatchString(expected) {
		return fmt.Errorf("invalid expected SHA-256 digest")
	}
	actual := sha256.Sum256(data)
	if hex.EncodeToString(actual[:]) != expected {
		return fmt.Errorf("artifact SHA-256 mismatch")
	}
	return nil
}
