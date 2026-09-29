// Package versioncache caches an agent runtime CLI's version and re-probes when the binary's size+mtime changes.
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

// Cache holds one runtime CLI's version and its binary stamp; construct with New.
type Cache struct {
	bin       string // absolute path, or a bare name resolved through PATH
	component string // slog "component" tag, e.g. "codex-probe"
	probe     func(context.Context) (version string, ok bool)

	version atomic.Pointer[string]
	stamp   atomic.Pointer[string]
	probing atomic.Bool
}

// New returns a cache for the CLI at bin (path or PATH name), probed by probe; component tags logs.
func New(bin, component string, probe func(context.Context) (string, bool)) *Cache {
	return &Cache{bin: bin, component: component, probe: probe}
}

// Get returns the cached version ("" if never probed) and re-probes in the background if the binary changed.
func (c *Cache) Get() string {
	c.refreshIfChanged()
	if v := c.version.Load(); v != nil {
		return *v
	}
	return ""
}

// Set overrides the cached version without probing.
func (c *Cache) Set(version string) {
	c.version.Store(&version)
	if s := binStamp(c.bin); s != "" {
		c.stamp.Store(&s)
	}
}

// Populate blocks running the boot probe with up to retries backoff attempts; call from a goroutine.
func (c *Cache) Populate(retries int, backoff time.Duration) {
	if binStamp(c.bin) == "" {
		return
	}
	admission := startup.Load()
	// Hold the probing flag for the whole loop so a concurrent Get cannot probe too.
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
		// Record the stamp even on failure so later Gets do not retry it.
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

// refreshIfChanged re-probes in the background when the stamp changed; at most one probe at a time.
func (c *Cache) refreshIfChanged() {
	stamp := binStamp(c.bin)
	if stamp == "" {
		return
	}
	if s := c.stamp.Load(); s != nil && *s == stamp {
		return
	}
	if !c.probing.CompareAndSwap(false, true) {
		return
	}
	// Claim this build before probing so a slow or failing probe is not retried on every Get.
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

// binStamp returns "<size>:<mtime>" of bin (following symlinks), or "" when unresolvable.
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
