// Package runtimereg is a dependency-free registry of embedded runtime scripts and version getters.
// It breaks the system/device -> runtimes/* import cycle; backends register from init().
package runtimereg

// Maps are populated only during init(), before any runtime switch, so reads need no locking.
var (
	installers = map[string][]byte{}
	presyncs   = map[string][]byte{}
	readiness  = map[string][]byte{}
	versions   = map[string]func() string{}
)

// Register records a backend's embedded installer; the last registration wins.
func Register(name string, script []byte) {
	installers[name] = script
}

// Get returns the embedded installer, or (nil, false) when the backend is not compiled in.
func Get(name string) ([]byte, bool) {
	s, ok := installers[name]
	return s, ok
}

// RegisterPresync records a backend's embedded pre-start hook (runtime-<name>-presync).
// os-server materializes it so an OTA refreshes the hook without reinstalling the backend.
func RegisterPresync(name string, script []byte) {
	presyncs[name] = script
}

// GetPresync returns the embedded pre-start hook, or (nil, false) when the backend ships none.
func GetPresync(name string) ([]byte, bool) {
	s, ok := presyncs[name]
	return s, ok
}

// RegisterReadiness records a backend readiness probe run after the unit becomes active, on request.
func RegisterReadiness(name string, script []byte) {
	readiness[name] = script
}

// GetReadiness returns a backend's readiness probe, if it provides one.
func GetReadiness(name string) ([]byte, bool) {
	s, ok := readiness[name]
	return s, ok
}

// RegisterVersion records a backend's cached-version getter (e.g. openclaw.GetOpenClawVersion).
func RegisterVersion(name string, fn func() string) {
	versions[name] = fn
}

// Version returns the backend's installed version, or "" when unknown.
func Version(name string) string {
	if fn, ok := versions[name]; ok {
		return fn()
	}
	return ""
}
