package http

import (
	"encoding/base64"
	"fmt"
	"io"
	"strings"
)

const (
	maxEventAttachments          = 4
	maxEventAttachmentBytes      = 10 << 20
	maxEventTotalAttachmentBytes = 20 << 20
	// Base64 plus JSON framing/message, bounded before JSON allocation.
	maxSensingBodyBytes = 29 << 20
)

func validateEventAttachments(req SensingEventRequest) error {
	if len(req.Images)+len(req.Files) > maxEventAttachments {
		return fmt.Errorf("at most %d attachments allowed", maxEventAttachments)
	}
	total := int64(0)
	validate := func(encoded string) error {
		if len(encoded) > base64.StdEncoding.EncodedLen(maxEventAttachmentBytes) {
			return fmt.Errorf("attachment exceeds 10 MiB")
		}
		n, err := io.Copy(io.Discard, base64.NewDecoder(base64.StdEncoding, strings.NewReader(encoded)))
		if err != nil {
			return fmt.Errorf("invalid attachment encoding: %w", err)
		}
		total += n
		if n > maxEventAttachmentBytes || total > maxEventTotalAttachmentBytes {
			return fmt.Errorf("attachments exceed size limit (10 MiB each, 20 MiB total)")
		}
		return nil
	}
	for _, image := range req.Images {
		if err := validate(image); err != nil {
			return err
		}
	}
	for _, file := range req.Files {
		if err := validate(file.Content); err != nil {
			return err
		}
	}
	return nil
}
