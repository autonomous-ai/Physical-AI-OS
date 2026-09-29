package http

import (
	"fmt"
	"os"
	"strings"
)

func voicePathComponent(name string) bool {
	return name != "" && name != "." && name != ".." && !strings.ContainsAny(name, "/\\\x00")
}

// Retain directory handles for deletion, so a concurrent rename or symlink swap
// cannot redirect sample/embedding removal outside the selected profile.
func openVoiceDirectory(base, name string) (*os.Root, error) {
	if !voicePathComponent(name) {
		return nil, fmt.Errorf("invalid voice profile name")
	}
	users, err := os.OpenRoot(base)
	if err != nil {
		return nil, err
	}
	defer users.Close()
	profile, err := users.OpenRoot(name)
	if err != nil {
		return nil, err
	}
	defer profile.Close()
	return profile.OpenRoot("voice")
}
