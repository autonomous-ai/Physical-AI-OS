package accountlogin

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func newStage() (string, error) { return os.MkdirTemp("", "runtime-login-") }

// Do not inherit credentials or routing from the OS service into a native login.
func cleanEnv(home string, overrides map[string]string) []string {
	var env []string
	for _, item := range os.Environ() {
		key, _, _ := strings.Cut(item, "=")
		if key == "HOME" || key == "BROWSER" || key == "TERM" || key == "NO_COLOR" || key == "CI" || key == "HERMES_HOME" || key == "CODEX_HOME" || key == "CLAUDE_CONFIG_DIR" || key == "OPENCLAW_STATE_DIR" || key == "OPENCLAW_CONFIG_PATH" || strings.HasPrefix(key, "HERMES_") || strings.HasPrefix(key, "XDG_") || strings.HasPrefix(key, "ANTHROPIC_") || strings.HasPrefix(key, "OPENAI_") || strings.HasPrefix(key, "CLAUDE_") || strings.HasPrefix(key, "CODEX_") || strings.HasPrefix(key, "OPENCLAW_") || strings.Contains(key, "API_KEY") || strings.Contains(key, "AUTH_TOKEN") {
			continue
		}
		if _, exists := overrides[key]; !exists {
			env = append(env, item)
		}
	}
	env = append(env, "HOME="+home, "BROWSER=/bin/true", "TERM=xterm", "NO_COLOR=1", "XDG_CONFIG_HOME="+filepath.Join(home, ".config"), "XDG_CACHE_HOME="+filepath.Join(home, ".cache"), "XDG_DATA_HOME="+filepath.Join(home, ".local", "share"), "XDG_STATE_HOME="+filepath.Join(home, ".local", "state"))
	for key, value := range overrides {
		env = append(env, key+"="+value)
	}
	return env
}

func readJSON(path string) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read runtime account file: %w", err)
	}
	var value map[string]any
	if err = json.Unmarshal(data, &value); err != nil || value == nil {
		return nil, fmt.Errorf("invalid runtime account JSON")
	}
	return value, nil
}

func mergeJSONKey(stagePath, targetPath, key string) ([]byte, error) {
	source, err := readJSON(stagePath)
	if err != nil {
		return nil, err
	}
	value, exists := source[key]
	if !exists || value == nil {
		return nil, fmt.Errorf("new credential is missing %s", key)
	}
	target, err := readJSON(targetPath)
	if err != nil {
		return nil, err
	}
	target[key] = value
	return json.MarshalIndent(target, "", "  ")
}

func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".account-login-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

// Snapshot every destination before the first write. Failed installs and failed
// activation restore the exact previous file contents (or absence).
func writeChanges(changes map[string][]byte) (func() error, error) {
	type snapshot struct {
		data   []byte
		exists bool
		mode   os.FileMode
	}
	originals := map[string]snapshot{}
	var paths []string
	for path := range changes {
		info, err := os.Lstat(path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if err == nil && !info.Mode().IsRegular() {
			return nil, fmt.Errorf("runtime account destination is not a regular file")
		}
		old := snapshot{}
		if err == nil {
			old.data, err = os.ReadFile(path)
			if err != nil {
				return nil, err
			}
			old.exists = true
			old.mode = info.Mode().Perm()
		}
		originals[path] = old
		paths = append(paths, path)
	}
	sort.Strings(paths)
	var written []string
	rollback := func() error {
		var errs []error
		for i := len(written) - 1; i >= 0; i-- {
			path := written[i]
			old := originals[path]
			var err error
			if old.exists {
				err = atomicWrite(path, old.data)
				if err == nil {
					err = os.Chmod(path, old.mode)
				}
			} else {
				err = os.Remove(path)
				if errors.Is(err, os.ErrNotExist) {
					err = nil
				}
			}
			errs = append(errs, err)
		}
		return errors.Join(errs...)
	}
	for _, path := range paths {
		if err := atomicWrite(path, changes[path]); err != nil {
			return rollback, errors.Join(err, rollback())
		}
		written = append(written, path)
	}
	return rollback, nil
}
