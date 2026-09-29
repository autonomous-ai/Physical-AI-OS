// Package versioncache caches an agent runtime CLI's version string and
// refreshes it when the binary on disk changes.
//
// Every runtime package (openclaw, hermes, picoclaw, codex, claudecode,
// opencode) used to keep its own atomic pointer filled once by a startup probe
// and never touched again, so a CLI updated under a running os-server — by the
// OTA worker running `software-update <runtime>`, or by a manual install over
// SSH — kept reporting the old version on the Status endpoint and the MQTT
// `info` message until os-server itself was restarted. The updater is a separate
// process, so there is nothing to notify os-server in-band; the binary's own
// size+mtime is the signal that survives every update path.
//
// Get() therefore stats the binary (cheap, no shell-out) and kicks an
// asynchronous re-probe when the stamp moved. The refreshed value lands within
// one poll interval instead of one restart.
package versioncache

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"time"
)

// Cache holds one runtime CLI's version plus the stamp of the binary it was
// probed from. The zero value is not usable — construct with New.
type Cache struct {
	bin       string // absolute path, or a bare name resolved through PATH
	component string // slog "component" tag, e.g. "codex-probe"
	probe     func(context.Context) (version string, ok bool)

	version atomic.Pointer[string]
	stamp   atomic.Pointer[string]
	probing atomic.Bool
}

// New returns a cache for the CLI at bin, probed by probe. component is the
// slog tag used for the give-up warning. bin may be a bare command name, in
// which case it is resolved through PATH on every stamp.
func New(bin, component string, probe func(context.Context) (string, bool)) *Cache {
	return &Cache{bin: bin, component: component, probe: probe}
}

// Get returns the cached version, or "" when no probe has succeeded yet. It
// also refreshes the cache in the background when the binary changed since the
// cached value was probed, so callers polling Get (Status endpoint, MQTT info)
// pick up an update without an os-server restart.
func (c *Cache) Get() string {
	c.refreshIfChanged()
	if v := c.version.Load(); v != nil {
		return *v
	}
	return ""
}

// Set overrides the cached version without probing. Only for tests and for
// callers that already know the version they installed.
func (c *Cache) Set(version string) {
	c.version.Store(&version)
	if s := binStamp(c.bin); s != "" {
		c.stamp.Store(&s)
	}
}

// Populate runs the boot-time probe loop: retry a killed/empty probe up to
// retries times with backoff between attempts, so a cold-start slowdown
// self-heals instead of leaving the version blank. Blocking — call it from a
// startup goroutine. Stops as soon as a version is stored.
func (c *Cache) Populate(retries int, backoff time.Duration) {
	if binStamp(c.bin) == "" {
		return
	}
	admission := startup.Load()
	// Hold the probing flag for the whole loop so a concurrent Get does not
	// fire a second shell-out on top of the retries.
	if !c.probing.CompareAndSwap(false, true) {
		return
	}
	defer c.probing.Store(false)

	for attempt := 0; ; attempt++ {
		if !admission.acquire() {
			return
		}
		stamp := binStamp(c.bin)
		if stamp == "" {
			admission.release()
			return
		}
		v, ok := c.probe(admission.ctx)
		admission.release()
		if admission.ctx.Err() != nil {
			return
		}
		// Record the stamp either way: a probe that fails because this runtime
		// is not the installed backend must not make every later Get retry it.
		if stamp != "" {
			c.stamp.Store(&stamp)
		}
		if ok {
			c.version.Store(&v)
			slog.Info("runtime version populated", "component", c.component, "version", v)
			return
		}
		if attempt >= retries {
			slog.Warn("read runtime version gave up after retries (expected if not on this backend)",
				"component", c.component, "attempts", attempt+1)
			return
		}
		timer := time.NewTimer(backoff)
		select {
		case <-admission.ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// refreshIfChanged re-probes in the background when the binary's stamp differs
// from the one the cached version came from. At most one probe runs at a time.
func (c *Cache) refreshIfChanged() {
	stamp := binStamp(c.bin)
	if stamp == "" {
		// Not installed (or not stat-able): keep whatever we have. An absent
		// binary is the normal case for the five runtimes that are not the
		// active backend.
		return
	}
	if s := c.stamp.Load(); s != nil && *s == stamp {
		return
	}
	if !c.probing.CompareAndSwap(false, true) {
		return
	}
	// Claim this build before probing. A probe can take seconds (Node CLIs) and
	// can fail; retrying it on every Get would spawn one shell-out per poll.
	// The next genuine change re-arms the refresh.
	c.stamp.Store(&stamp)
	admission := startup.Load()
	go func() {
		defer c.probing.Store(false)
		if !admission.acquire() {
			return
		}
		defer admission.release()
		stamp := binStamp(c.bin)
		if stamp == "" {
			return
		}
		c.stamp.Store(&stamp)
		if v, ok := c.probe(admission.ctx); ok && admission.ctx.Err() == nil {
			c.version.Store(&v)
			slog.Info("runtime version refreshed after binary change",
				"component", c.component, "version", v)
		}
	}()
}

// binStamp identifies the current build of bin as "<size>:<mtime>". Empty when
// the binary cannot be resolved or stat'ed. os.Stat follows symlinks on purpose:
// installers commonly repoint /usr/local/bin/<cli> at a new versioned target,
// and the target — not the link — is what actually changed.
func binStamp(bin string) string {
	path := bin
	if !strings.ContainsRune(bin, os.PathSeparator) {
		p, err := exec.LookPath(bin)
		if err != nil {
			return ""
		}
		path = p
	}
	fi, err := os.Stat(path)
	if err != nil {
		return ""
	}
	return fmt.Sprintf("%d:%d", fi.Size(), fi.ModTime().UnixNano())
}
