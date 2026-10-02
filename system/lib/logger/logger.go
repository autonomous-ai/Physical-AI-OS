package logger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"gopkg.in/natefinch/lumberjack.v2"
)

// GELF settings, read in Init so callers can load .env first.
var (
	gelfURL      string
	gelfUsername string
	gelfPassword string
)

const (
	colorReset  = "\033[0m"
	colorRed    = "\033[31m"
	colorGreen  = "\033[32m"
	colorYellow = "\033[33m"
	colorCyan   = "\033[36m"
	colorGray   = "\033[90m"
)

// colorHandler is a slog.Handler that writes colored, human-readable log lines to the console.
type colorHandler struct {
	w     io.Writer
	mu    sync.Mutex
	level slog.Level
	attrs []slog.Attr
	group string
}

func (h *colorHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

func (h *colorHandler) Handle(_ context.Context, r slog.Record) error {
	var levelColor, levelTag string
	switch {
	case r.Level >= slog.LevelError:
		levelColor = colorRed
		levelTag = "ERROR"
	case r.Level >= slog.LevelWarn:
		levelColor = colorYellow
		levelTag = "WARN"
	case r.Level >= slog.LevelInfo:
		levelColor = colorGreen
		levelTag = "INFO"
	default:
		levelColor = colorGray
		levelTag = "DEBUG"
	}

	ts := r.Time.Format(time.DateTime)
	line := fmt.Sprintf("%s%s%s %s%-5s%s %s",
		colorGray, ts, colorReset,
		levelColor, levelTag, colorReset,
		r.Message,
	)

	r.Attrs(func(a slog.Attr) bool {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		line += fmt.Sprintf(" %s%s=%s%v", colorCyan, key, colorReset, a.Value)
		return true
	})
	for _, a := range h.attrs {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		line += fmt.Sprintf(" %s%s=%s%v", colorCyan, key, colorReset, a.Value)
	}
	line += "\n"

	h.mu.Lock()
	defer h.mu.Unlock()
	_, err := h.w.Write([]byte(line))
	return err
}

func (h *colorHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs), len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	newAttrs = append(newAttrs, attrs...)
	return &colorHandler{w: h.w, level: h.level, attrs: newAttrs, group: h.group}
}

func (h *colorHandler) WithGroup(name string) slog.Handler {
	g := name
	if h.group != "" {
		g = h.group + "." + name
	}
	newAttrs := make([]slog.Attr, len(h.attrs))
	copy(newAttrs, h.attrs)
	return &colorHandler{w: h.w, level: h.level, attrs: newAttrs, group: g}
}

// multiHandler fans out each log record to multiple handlers.
type multiHandler struct {
	handlers []slog.Handler
}

func (m *multiHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (m *multiHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, h := range m.handlers {
		if h.Enabled(ctx, r.Level) {
			if err := h.Handle(ctx, r); err != nil {
				return err
			}
		}
	}
	return nil
}

func (m *multiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithAttrs(attrs)
	}
	return &multiHandler{handlers: handlers}
}

func (m *multiHandler) WithGroup(name string) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithGroup(name)
	}
	return &multiHandler{handlers: handlers}
}

const (
	gelfQueueSize            = 256
	gelfShutdownFlushTimeout = 5 * time.Second
	// gelfRelayPath is appended to the cloud API base URL (already ending in /v1).
	gelfRelayPath = "/logs/gelf"

	// gelfPreConfigHost is the host a record carries before config.json supplies
	// the device id; replay moves such records onto the device's real host.
	gelfPreConfigHost = "os-server"
)

// Replay timing. Variables, not constants, so tests can shorten them.
var (
	// gelfReplayInterval paces spool replay. The cloud relay answers 202 at once
	// and silently drops anything past its in-flight ceiling, so a backlog sent
	// at full speed would be lost without the device ever knowing.
	gelfReplayInterval = 100 * time.Millisecond
	// gelfRetryMin/Max bound the backoff between failed replay attempts.
	gelfRetryMin = 5 * time.Second
	gelfRetryMax = 5 * time.Minute
	// gelfSpoolCheckInterval catches records that reached the spool after a
	// replay finished (a send failure, or a race with the backlog flag).
	gelfSpoolCheckInterval = 30 * time.Second
)

// sendResult says what to do with a record after one delivery attempt.
type sendResult int

const (
	sendOK    sendResult = iota
	sendRetry            // transient (network, 5xx, 401/403/408/429): keep it
	sendDrop             // the collector will never take it (e.g. 413): skip it
)

// gelfSender owns the bounded GELF queue; overload drops the newest record, with
// exponentially spaced stderr notices, so a slow collector never spawns unbounded goroutines.
// With a spool, overload and failed sends go to disk and are replayed in order.
type gelfSender struct {
	client *http.Client
	url    string
	// key is the relay credential this sender was armed with (empty for a
	// direct collector), so EnableGELFRelay can tell a no-op from a re-target.
	key string
	// auth sets the request credential (basic or Bearer); nil sends none.
	auth func(*http.Request)
	// spool is the on-disk fallback; nil means records that cannot be sent are
	// dropped (direct collector, tests).
	spool *gelfSpool
	// host returns the device's current GELF host, used when replaying records
	// logged before the device id was known.
	host func() string

	queue   chan []byte
	ctx     context.Context
	cancel  context.CancelFunc
	done    chan struct{}
	mu      sync.RWMutex
	closed  bool
	dropped atomic.Uint64
	// backlog routes new records to the spool while older ones are still
	// waiting there, so delivery order is preserved.
	backlog atomic.Bool
	wake    chan struct{}
	// stopping ends a spooled sender between records, never mid-request.
	stopping chan struct{}
}

func newGELFSender(client *http.Client, url string, auth func(*http.Request)) *gelfSender {
	return newSpooledGELFSender(client, url, auth, nil, nil)
}

func newSpooledGELFSender(client *http.Client, url string, auth func(*http.Request), spool *gelfSpool, host func() string) *gelfSender {
	ctx, cancel := context.WithCancel(context.Background())
	s := &gelfSender{
		client:   client,
		url:      url,
		auth:     auth,
		spool:    spool,
		host:     host,
		queue:    make(chan []byte, gelfQueueSize),
		ctx:      ctx,
		cancel:   cancel,
		done:     make(chan struct{}),
		wake:     make(chan struct{}, 1),
		stopping: make(chan struct{}),
	}
	if spool != nil && spool.pending() {
		s.backlog.Store(true)
	}
	go s.run()
	return s
}

// basicAuth is the direct-collector credential; nil without a username.
func basicAuth(username, password string) func(*http.Request) {
	if username == "" {
		return nil
	}
	return func(r *http.Request) { r.SetBasicAuth(username, password) }
}

// bearerAuth is the relay credential: the device's lobster API key.
func bearerAuth(key string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+key) }
}

func (s *gelfSender) enqueue(body []byte) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.closed {
		if s.spool != nil {
			s.spool.append(body)
		}
		return
	}
	if s.spool != nil && s.backlog.Load() {
		s.spool.append(body)
		s.nudge()
		return
	}
	select {
	case s.queue <- body:
	default:
		if s.spool != nil {
			s.spool.append(body)
			s.backlog.Store(true)
			s.nudge()
			return
		}
		dropped := s.dropped.Add(1)
		if dropped == 1 || dropped&(dropped-1) == 0 {
			fmt.Fprintf(os.Stderr, "[gelf] queue full; dropped %d record(s)\n", dropped)
		}
	}
}

func (s *gelfSender) nudge() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *gelfSender) run() {
	defer close(s.done)
	check := time.NewTicker(gelfSpoolCheckInterval)
	defer check.Stop()
	backoff := gelfRetryMin
	for {
		select {
		case <-s.stopping:
			return
		default:
		}
		if s.spool != nil && s.backlog.Load() {
			if !s.drainSpool() {
				if !s.sleep(backoff) {
					return
				}
				backoff = min(backoff*2, gelfRetryMax)
				continue
			}
			backoff = gelfRetryMin
			s.backlog.Store(false)
		}
		select {
		case <-s.ctx.Done():
			return
		case <-s.stopping:
			return
		case body, ok := <-s.queue:
			if !ok {
				return
			}
			if s.send(body) == sendRetry && s.spool != nil {
				s.spoolFailed(body)
			}
		case <-s.wake:
		case <-check.C:
			if s.spool != nil && s.spool.pending() {
				s.backlog.Store(true)
			}
		}
	}
}

// spoolFailed moves a record that could not be sent to the spool, followed by
// every record queued behind it. The write lock keeps enqueue out meanwhile:
// otherwise a record logged after the failure reaches the spool first and is
// replayed ahead of the older ones still in the queue.
func (s *gelfSender) spoolFailed(body []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.spool.append(body)
	s.backlog.Store(true)
	for {
		select {
		case queued, ok := <-s.queue:
			if !ok {
				return
			}
			s.spool.append(queued)
		default:
			return
		}
	}
}

// drainSpool replays everything spooled, oldest first and paced. It returns
// true once the spool is empty, false when a send failed (the undelivered
// records stay on disk for the next attempt) or the sender is shutting down.
func (s *gelfSender) drainSpool() bool {
	for s.spool.pending() {
		if err := s.spool.take(); err != nil {
			fmt.Fprintf(os.Stderr, "[gelf] spool take: %v\n", err)
			return false
		}
		lines, err := s.spool.replayLines()
		if err != nil {
			fmt.Fprintf(os.Stderr, "[gelf] spool read: %v\n", err)
			return false
		}
		host := ""
		if s.host != nil {
			host = s.host()
		}
		for i, line := range lines {
			body := prepareReplay(line, gelfPreConfigHost, host)
			if body != nil && s.send(body) == sendRetry {
				s.keepReplay(lines[i:])
				return false
			}
			if !s.sleep(gelfReplayInterval) {
				s.keepReplay(lines[i+1:])
				return false
			}
		}
		s.keepReplay(nil)
	}
	return true
}

func (s *gelfSender) keepReplay(rest [][]byte) {
	if err := s.spool.keepReplay(rest); err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] spool keep: %v\n", err)
	}
}

// sleep waits d, returning false if the sender is shutting down.
func (s *gelfSender) sleep(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-s.ctx.Done():
		return false
	case <-s.stopping:
		return false
	case <-t.C:
		return true
	}
}

func (s *gelfSender) send(body []byte) sendResult {
	req, err := http.NewRequestWithContext(s.ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] request error: %v\n", err)
		return sendDrop
	}
	req.Header.Set("Content-Type", "application/json")
	if s.auth != nil {
		s.auth(req)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		if s.ctx.Err() == nil {
			fmt.Fprintf(os.Stderr, "[gelf] send error: %v\n", err)
		}
		return sendRetry
	}
	resp.Body.Close()
	switch code := resp.StatusCode; {
	case code >= 200 && code < 300:
		return sendOK
	case code == http.StatusUnauthorized, code == http.StatusForbidden,
		code == http.StatusRequestTimeout, code == http.StatusTooManyRequests, code >= 500:
		// A key the cloud does not know yet (or any more) is kept, not dropped:
		// a later setup re-targets the relay with a key it accepts.
		return sendRetry
	default:
		return sendDrop
	}
}

func (s *gelfSender) close() {
	defer s.cancel()
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	close(s.queue)
	s.mu.Unlock()

	if s.spool != nil {
		// Stop after the in-flight send (bounded by the client timeout), not
		// during it: aborting a request the collector may already have
		// accepted would replay that record again. Queued records land in the
		// spool; the next sender (or the next boot) delivers them.
		close(s.stopping)
		<-s.done
		for body := range s.queue {
			s.spool.append(body)
		}
		return
	}

	timer := time.NewTimer(gelfShutdownFlushTimeout)
	defer timer.Stop()
	select {
	case <-s.done:
	case <-timer.C:
		remaining := len(s.queue)
		s.cancel()
		<-s.done
		fmt.Fprintf(os.Stderr, "[gelf] shutdown flush timed out; dropped %d queued record(s)\n", remaining)
	}
}

// gelfSink is the slot shared by a GELF handler and all its With/WithGroup copies
// (sender, spool and record identity), so arming or re-identifying reaches
// loggers built before config loaded.
type gelfSink struct {
	sender atomic.Pointer[gelfSender]
	spool  atomic.Pointer[gelfSpool]
	meta   atomic.Pointer[gelfMeta]
	// direct is set when GELF_URL was configured: direct delivery wins and the
	// relay never re-targets it.
	direct bool
}

// gelfMeta is the identity stamped on every record.
type gelfMeta struct {
	host       string
	deviceType string
	service    string
}

func (s *gelfSink) load() *gelfSender { return s.sender.Load() }

func (s *gelfSink) identity() gelfMeta {
	if m := s.meta.Load(); m != nil {
		return *m
	}
	return gelfMeta{host: gelfPreConfigHost, service: gelfPreConfigHost}
}

// update applies fn to a copy of the identity and publishes it.
func (s *gelfSink) update(fn func(*gelfMeta)) {
	for {
		old := s.meta.Load()
		next := s.identity()
		fn(&next)
		if s.meta.CompareAndSwap(old, &next) {
			return
		}
	}
}

// gelfHandler enqueues records for the shared GELF sender, or spools them while
// there is none; dormant when it has neither.
type gelfHandler struct {
	level  slog.Level
	client *http.Client
	sink   *gelfSink
	attrs  []slog.Attr
	group  string
}

// newGELFHandler returns a handler delivering straight to GELF_URL.
func newGELFHandler(level slog.Level, host string) *gelfHandler {
	h := newDormantGELFHandler(level, host)
	h.sink.direct = true
	h.sink.sender.Store(newGELFSender(h.client, gelfURL, basicAuth(gelfUsername, gelfPassword)))
	return h
}

// newDormantGELFHandler returns a handler with no sender for EnableGELFRelay to arm later;
// until then records go to the spool, if one is configured.
func newDormantGELFHandler(level slog.Level, host string) *gelfHandler {
	h := &gelfHandler{
		level:  level,
		client: &http.Client{Timeout: 3 * time.Second},
		sink:   &gelfSink{},
	}
	h.sink.update(func(m *gelfMeta) { m.host = host })
	return h
}

func (h *gelfHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level && (h.sink.load() != nil || h.sink.spool.Load() != nil)
}

func slogLevelToGELF(level slog.Level) int {
	switch {
	case level >= slog.LevelError:
		return 3 // error
	case level >= slog.LevelWarn:
		return 4 // warning
	case level >= slog.LevelInfo:
		return 6 // info
	default:
		return 7 // debug
	}
}

func (h *gelfHandler) Handle(_ context.Context, r slog.Record) error {
	sender := h.sink.load()
	spool := h.sink.spool.Load()
	if sender == nil && spool == nil {
		return nil // dormant: nowhere to ship or keep it
	}

	id := h.sink.identity()
	msg := map[string]any{
		"version":       "1.1",
		"host":          id.host,
		"short_message": r.Message,
		"timestamp":     float64(r.Time.UnixNano()) / 1e9,
		"level":         slogLevelToGELF(r.Level),
		"_service_name": id.service,
		"_level_name":   r.Level.String(),
		"_pid":          os.Getpid(),
	}
	if id.deviceType != "" {
		msg["_device_type"] = id.deviceType // device class, for centralized filtering
	}

	for _, a := range h.attrs {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		msg["_"+key] = a.Value.String()
	}
	r.Attrs(func(a slog.Attr) bool {
		key := a.Key
		if h.group != "" {
			key = h.group + "." + key
		}
		msg["_"+key] = a.Value.String()
		return true
	})

	body, err := json.Marshal(msg)
	if err != nil {
		return nil // don't block on marshal errors
	}

	if sender != nil {
		sender.enqueue(body)
	} else {
		spool.append(body)
	}
	return nil
}

func (h *gelfHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs), len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	newAttrs = append(newAttrs, attrs...)
	return &gelfHandler{level: h.level, client: h.client, sink: h.sink, attrs: newAttrs, group: h.group}
}

func (h *gelfHandler) WithGroup(name string) slog.Handler {
	g := name
	if h.group != "" {
		g = h.group + "." + name
	}
	newAttrs := make([]slog.Attr, len(h.attrs))
	copy(newAttrs, h.attrs)
	return &gelfHandler{level: h.level, client: h.client, sink: h.sink, attrs: newAttrs, group: g}
}

// activeGELF holds the GELF handler so SetGELFHost can update host after config loads.
var activeGELF *gelfHandler

// SetGELFHost updates the GELF host field (call after config is loaded with device_id).
func SetGELFHost(host string) {
	if activeGELF != nil && host != "" {
		activeGELF.sink.update(func(m *gelfMeta) { m.host = host })
	}
}

// SetGELFDeviceType stamps the device class on every shipped log as `_device_type`.
func SetGELFDeviceType(deviceType string) {
	if activeGELF != nil && deviceType != "" {
		activeGELF.sink.update(func(m *gelfMeta) { m.deviceType = deviceType })
	}
}

// SetGELFServiceName sets `_service_name` on every record (default "os-server").
func SetGELFServiceName(name string) {
	if activeGELF != nil && name != "" {
		activeGELF.sink.update(func(m *gelfMeta) { m.service = name })
	}
}

// EnableGELFSpool keeps records that cannot ship yet in dir (one bounded spool
// per service) and replays them once the relay delivers. Call right after Init:
// a first setup runs with no key and no internet. An unwritable dir disables it.
func EnableGELFSpool(dir, name string) {
	h := activeGELF
	if h == nil || h.sink.direct || strings.TrimSpace(dir) == "" || strings.TrimSpace(name) == "" {
		return
	}
	spool, err := newGELFSpool(dir, name, gelfSpoolMaxBytes)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[gelf] spool disabled: %v\n", err)
		return
	}
	h.sink.spool.Store(spool)
}

// relayMu serializes EnableGELFRelay, so only one re-target runs at a time.
var relayMu sync.Mutex

// EnableGELFRelay arms the dormant handler to POST {baseURL}/logs/gelf with apiKey as Bearer.
// Call whenever config.json reloads: the same target is a no-op, a new URL or key
// re-targets without dropping queued records. No-op when GELF_URL is set or inputs are blank.
func EnableGELFRelay(baseURL, apiKey string) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	apiKey = strings.TrimSpace(apiKey)
	h := activeGELF
	if h == nil || h.sink.direct || baseURL == "" || apiKey == "" {
		return
	}
	relayMu.Lock()
	defer relayMu.Unlock()
	target := baseURL + gelfRelayPath
	sink := h.sink
	old := sink.load()
	if old != nil && old.url == target && old.key == apiKey {
		return
	}
	if old != nil {
		// Stop the old sender before the new one starts: both replaying the
		// same spool would ship its records twice. Records logged meanwhile
		// go to the spool (no sender) and the new sender replays them.
		sink.sender.Store(nil)
		old.close()
	}
	sender := newSpooledGELFSender(h.client, target, bearerAuth(apiKey), sink.spool.Load(),
		func() string { return sink.identity().host })
	sender.key = apiKey
	sink.sender.Store(sender)
	if spool := sink.spool.Load(); spool != nil && spool.pending() {
		sender.backlog.Store(true) // records spooled while no sender was set
		sender.nudge()
	}
}

// Init sets up the default slog logger (level from HAL_LOG_LEVEL, default INFO), optionally
// also writing to logFilePath; the returned func closes the file.
func Init(logFilePath string) func() {
	level := levelFromEnv()

	consoleHandler := &colorHandler{
		w:     os.Stdout,
		level: level,
	}

	if logFilePath == "" {
		slog.SetDefault(slog.New(consoleHandler))
		return func() {}
	}

	rotatingWriter := &lumberjack.Logger{
		Filename:   logFilePath,
		MaxSize:    2, // MB
		MaxBackups: 10,
		MaxAge:     0, // no age-based removal
		Compress:   false,
	}

	fileHandler := &colorHandler{
		w:     rotatingWriter,
		level: level,
	}

	gelfURL = os.Getenv("GELF_URL")
	gelfUsername = os.Getenv("GELF_USERNAME")
	gelfPassword = os.Getenv("GELF_PASSWORD")

	// Without GELF_URL the handler stays dormant until EnableGELFRelay arms it.
	var gelf *gelfHandler
	if gelfURL != "" {
		gelf = newGELFHandler(level, "os-server")
	} else {
		gelf = newDormantGELFHandler(level, "os-server")
	}
	activeGELF = gelf

	slog.SetDefault(slog.New(&multiHandler{handlers: []slog.Handler{consoleHandler, fileHandler, gelf}}))

	return func() {
		if activeGELF == gelf {
			activeGELF = nil
		}
		if sender := gelf.sink.load(); sender != nil {
			sender.close()
		}
		rotatingWriter.Close()
	}
}

func levelFromEnv() slog.Level {
	switch strings.ToUpper(strings.TrimSpace(os.Getenv("HAL_LOG_LEVEL"))) {
	case "DEBUG":
		return slog.LevelDebug
	case "INFO":
		return slog.LevelInfo
	case "WARN", "WARNING":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
