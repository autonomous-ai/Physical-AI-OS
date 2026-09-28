// Package harness connects one device to one explicitly paired Harness computer.
package harness

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type Frame map[string]any
type Callbacks struct {
	OnEvent func(Frame)
	// BeforeEvent persists correlated results before advancing the replay cursor.
	BeforeEvent func(Frame, ResultContext) error
	// BeforeRequest binds local intent to the exact authenticated connection.
	BeforeRequest func(Frame, ResultContext) error
	OnReceipt     func(Frame, ResultContext)
	// OnRevoked runs after the paired computer revoked this device and the pin
	// was removed (same cleanup as a local unpair, e.g. disable Harness voice).
	OnRevoked func()
}
type Status struct {
	Code             string   `json:"code,omitempty"`
	ExpiresAt        int64    `json:"expires_at,omitempty"`
	InstanceID       string   `json:"instance_id"`
	Revision         uint64   `json:"revision"`
	Pairing          bool     `json:"pairing"`
	Paired           bool     `json:"paired"`
	Connected        bool     `json:"connected"`
	State            string   `json:"state"`
	MachineID        string   `json:"machine_id,omitempty"`
	MachineName      string   `json:"machine_name,omitempty"`
	Fingerprint      string   `json:"fingerprint,omitempty"`
	ServerInstanceID string   `json:"server_instance_id,omitempty"`
	Error            string   `json:"error,omitempty"`
	Capabilities     []string `json:"capabilities"`
}
type peer struct {
	Protocol         string `json:"protocol"`
	MachineID        string `json:"machineId"`
	MachineName      string `json:"machineName"`
	PublicKey        string `json:"publicKey"`
	ProvisionalUntil int64  `json:"provisionalUntil,omitempty"`
}
type diskState struct {
	Version int    `json:"version"`
	Seed    string `json:"seed"`
	Peer    *peer  `json:"peer,omitempty"`
}
type response struct {
	frame Frame
	err   error
}
type connection struct {
	channel       *DirectChannel
	mu            sync.Mutex
	capabilities  map[string]bool
	pending       map[string]chan response
	resultContext ResultContext
}

// ResultContext is transport-owned; no event field can choose its owner.
type ResultContext struct {
	Owner            string
	MachineID        string
	ServerInstanceID string
}

func (s *Service) ResultContext() ResultContext {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == nil {
		return ResultContext{}
	}
	return s.conn.resultContext
}

// ResultOwner identifies the saved pair even while disconnected. Unpairing
// removes access to that owner's locally retained results.
func (s *Service) ResultOwner() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.disk.Peer == nil {
		return ""
	}
	pub, err := decode(s.disk.Peer.PublicKey, 32)
	if err != nil {
		return ""
	}
	identity := sha256.Sum256(append(append([]byte{}, s.identity.Public().(ed25519.PublicKey)...), pub...))
	return hex.EncodeToString(identity[:])
}

// DeliveryUnknownError means a task/focus mutation may have reached the computer.
// Never retry it automatically; preparation has separate durable recovery semantics.
type DeliveryUnknownError struct {
	RequestID string
	Cause     error
}

func (e *DeliveryUnknownError) Error() string {
	return "Harness delivery could not be confirmed; inspect its receipt before retrying"
}
func (e *DeliveryUnknownError) Unwrap() error { return e.Cause }

type PairInfo struct {
	Code      string `json:"code,omitempty"`
	ExpiresAt int64  `json:"expires_at,omitempty"`
	Pairing   bool   `json:"pairing"`
	State     string `json:"state"`
	MachineID string `json:"machine_id,omitempty"`
	Error     string `json:"error,omitempty"`
}
type pairAttempt struct {
	ctx         context.Context
	channel     *DirectChannel
	info        PairInfo
	cancel      context.CancelFunc
	generation  uint64
	provisional bool
}

type Service struct {
	mu            sync.Mutex
	attempt       *pairAttempt
	path          string
	disk          diskState
	identity      ed25519.PrivateKey
	status        Status
	conn          *connection
	callbacks     Callbacks
	ctx           context.Context
	sockets       map[*DirectChannel]bool
	incoming      int
	events        chan Frame
	started       bool
	generation    uint64
	server        string
	cursor        uint64
	instanceID    string
	revision      uint64
	statusChanges chan struct{}
}

var capabilities = []string{"focus.ensure", "focus.get", "focus.step", "agents.list", "turn.send", "turn.stop", "status", "recap", "question.answer", "receipt.get", "store.list", "store.inspect", "agent.prepare", "operation.get"}

func NewService(dataDir string, callbacks Callbacks) (*Service, error) {
	s := &Service{path: filepath.Join(dataDir, "harness", "trust.json"), callbacks: callbacks, ctx: context.Background(), sockets: make(map[*DirectChannel]bool), events: make(chan Frame, 128), statusChanges: make(chan struct{}, 1), status: Status{State: "unpaired", Capabilities: []string{}}}
	id, _ := randomBytes(8)
	s.instanceID = hex.EncodeToString(id)
	if err := safeTrustPath(s.path); err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(s.path)
	if err == nil {
		if err = json.Unmarshal(raw, &s.disk); err != nil {
			return nil, fmt.Errorf("read harness trust: %w", err)
		}
		if s.disk.Version != 1 {
			return nil, errors.New("unsupported harness trust version")
		}
		seed, e := decode(s.disk.Seed, 32)
		if e != nil {
			return nil, e
		}
		s.identity = ed25519.NewKeyFromSeed(seed)
		if s.disk.Peer != nil {
			if _, e = decode(s.disk.Peer.PublicKey, 32); e != nil {
				return nil, e
			}
			s.status.State = "disconnected"
		}
	} else if os.IsNotExist(err) {
		_, s.identity, err = ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return nil, err
		}
		s.disk = diskState{Version: 1, Seed: b64(s.identity.Seed())}
		if err = s.saveLocked(); err != nil {
			return nil, err
		}
	} else {
		return nil, fmt.Errorf("read harness trust: %w", err)
	}
	return s, nil
}
func safeTrustPath(path string) error {
	for _, p := range []string{filepath.Dir(path), path} {
		info, err := os.Lstat(p)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return errors.New("Harness trust path must not be a symlink")
		}
		if p == path && !info.Mode().IsRegular() {
			return errors.New("Harness trust must be a regular file")
		}
		if p != path && !info.IsDir() {
			return errors.New("Harness trust directory is invalid")
		}
	}
	return nil
}
func (s *Service) saveLocked() error {
	if err := safeTrustPath(s.path); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0700); err != nil {
		return fmt.Errorf("create harness directory: %w", err)
	}
	if err := os.Chmod(filepath.Dir(s.path), 0700); err != nil {
		return err
	}
	raw, err := json.Marshal(s.disk)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(s.path), ".trust-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(raw)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("write harness trust: %w", err)
	}
	if err = os.Rename(name, s.path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(s.path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// expireProvisionalLocked is used by both UI reads and connection admission.
// A failed durable deletion keeps the pin in memory and surfaces the failure.
func (s *Service) expireProvisionalLocked() error {
	p := s.disk.Peer
	if p == nil || p.ProvisionalUntil == 0 || time.Now().UnixMilli() < p.ProvisionalUntil {
		return nil
	}
	s.disk.Peer = nil
	if err := s.saveLocked(); err != nil {
		s.disk.Peer = p
		s.status.Error = "Expire Harness provisional trust: " + err.Error()
		return errors.New(s.status.Error)
	}
	s.generation++
	if s.conn == nil {
		s.status.State = "unpaired"
	}
	s.status.Error = ""
	if s.attempt != nil && s.attempt.provisional && !s.attempt.info.Pairing {
		s.attempt.info.State = "expired"
		s.attempt.info.Code = ""
	}
	return nil
}

func (s *Service) Status() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.expireProvisionalLocked() // Failure is retained in status.Error.
	v := s.status
	v.InstanceID = s.instanceID
	v.Revision = s.revision
	if s.attempt != nil && s.attempt.info.Pairing {
		v.Pairing = true
		v.Code = s.attempt.info.Code
		v.ExpiresAt = s.attempt.info.ExpiresAt
	} else {
		v.Pairing = false
		v.Code = ""
		v.ExpiresAt = 0
	}
	v.Capabilities = append([]string{}, v.Capabilities...)
	if p := s.disk.Peer; p != nil {
		v.Paired = p.ProvisionalUntil == 0

		v.MachineID = p.MachineID
		v.MachineName = p.MachineName
		pub, _ := decode(p.PublicKey, 32)
		h := sha256.Sum256(pub)
		x := strings.ToUpper(hex.EncodeToString(h[:8]))
		v.Fingerprint = x[:4] + "·" + x[4:8] + "·" + x[8:12] + "·" + x[12:]
	}
	return v
}
func (s *Service) StatusChanges() <-chan struct{} { return s.statusChanges }
func (s *Service) statusChangedLocked() {
	s.revision++
	select {
	case s.statusChanges <- struct{}{}:
	default:
	}
}
func stringField(f Frame, k string) string { v, _ := f[k].(string); return v }
func number(f Frame, k string) uint64      { n, _ := sessionCounter(f[k]); return n }

func (s *Service) PairStatus() PairInfo {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.attempt == nil {
		return PairInfo{State: "idle"}
	}
	return s.attempt.info
}

// Closing the pairing context closes its socket, which cancels the pending CLI intent.
func (s *Service) CancelPair() error {
	s.mu.Lock()
	if s.attempt == nil || !s.attempt.info.Pairing {
		s.mu.Unlock()
		return nil
	}
	attempt := s.attempt
	s.generation++
	attempt.info.Code = ""
	attempt.info.Pairing = false
	attempt.info.State = "cancelled"
	s.status.Pairing = false
	var err error
	if attempt.provisional && s.disk.Peer != nil && s.disk.Peer.ProvisionalUntil != 0 {
		previous := s.disk.Peer
		s.disk.Peer = nil
		err = s.saveLocked()
		if err != nil {
			s.disk.Peer = previous
			s.status.Error = "Cancel Harness pairing: " + err.Error()
		}
	}
	if s.disk.Peer == nil {
		s.status.State = "unpaired"
	} else if !s.status.Connected {
		s.status.State = "disconnected"
	}
	s.statusChangedLocked()
	s.mu.Unlock()
	attempt.cancel()
	return err
}

func mutation(kind string) bool {
	return kind == "turn.send" || kind == "turn.stop" || kind == "question.answer" || kind == "focus.step" || kind == "agent.prepare"
}

// Request sends exactly once. The caller owns stable idempotency keys for mutations.
func (s *Service) Request(ctx context.Context, frame Frame) (Frame, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	c := s.conn
	machine := ""
	if s.disk.Peer != nil {
		machine = s.disk.Peer.MachineID
	}
	s.mu.Unlock()
	if c == nil {
		return nil, errors.New("Harness is not connected")
	}
	kind := stringField(frame, "type")
	if !c.capabilities[kind] {
		return nil, errors.New("UNSUPPORTED_CAPABILITY")
	}
	f := Frame{}
	for k, v := range frame {
		f[k] = v
	}
	if isStoreOperation(kind) {
		for _, capability := range storeCapabilities {
			if !c.capabilities[capability] {
				return nil, errors.New("UNSUPPORTED_CAPABILITY: update Harness CLI for Store preparation")
			}
		}
		if kind == "agent.prepare" && stringField(f, "machineId") != machine {
			return nil, errors.New("MACHINE_MISMATCH")
		}
	} else if kind != "focus.ensure" && kind != "focus.get" && kind != "focus.step" && kind != "agents.list" && kind != "receipt.get" {
		if stringField(f, "machineId") != machine {
			return nil, errors.New("MACHINE_MISMATCH")
		}
		if stringField(f, "agentId") == "" {
			return nil, errors.New("MISSING_TARGET")
		}
	}
	if mutation(kind) && stringField(f, "idempotencyKey") == "" {
		return nil, errors.New("mutation requires idempotencyKey")
	}
	if kind == "turn.send" {
		text := stringField(f, "text")
		if len(text) == 0 || len(text) > 16384 {
			return nil, errors.New("PAYLOAD_TOO_LARGE")
		}
	}
	if isStoreOperation(kind) {
		if value, exists := f["requestId"]; exists {
			id, ok := value.(string)
			if !ok || !storeUUID.MatchString(id) {
				return nil, errors.New("INVALID_REQUEST: invalid Store requestId")
			}
		}
	}
	if stringField(f, "requestId") == "" {
		raw, err := randomBytes(16)
		if err != nil {
			return nil, err
		}
		raw[6] = (raw[6] & 15) | 64
		raw[8] = (raw[8] & 63) | 128
		h := hex.EncodeToString(raw)
		f["requestId"] = h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
	}
	if isStoreOperation(kind) {
		if err := validateStoreRequest(f); err != nil {
			return nil, err
		}
	}
	id := stringField(f, "requestId")
	ch := make(chan response, 1)
	c.mu.Lock()
	if len(c.pending) >= 64 {
		c.mu.Unlock()
		return nil, errors.New("too many pending Harness requests")
	}
	if c.pending[id] != nil {
		c.mu.Unlock()
		return nil, errors.New("requestId already pending")
	}
	c.pending[id] = ch
	c.mu.Unlock()
	defer func() { c.mu.Lock(); delete(c.pending, id); c.mu.Unlock() }()
	unknown := func(err error) (Frame, error) {
		if kind == "agent.prepare" {
			return nil, &PreparationUnknownError{RequestID: id, Cause: err}
		}
		if mutation(kind) {
			return nil, &DeliveryUnknownError{RequestID: id, Cause: err}
		}
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if s.callbacks.BeforeRequest != nil {
		if err := s.callbacks.BeforeRequest(f, c.resultContext); err != nil {
			return nil, err
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := c.channel.SendEncrypted(Frame{"type": "autonomous_device_request", "payload": f}); err != nil {
		return unknown(err)
	}
	timer := time.NewTimer(30 * time.Second)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return unknown(ctx.Err())
	case <-timer.C:
		return unknown(errors.New("Harness request timed out"))
	case r := <-ch:
		if r.err != nil {
			return unknown(r.err)
		}
		if s.callbacks.OnReceipt != nil {
			s.callbacks.OnReceipt(r.frame, c.resultContext)
		}
		return r.frame, nil
	}
}

func (s *Service) handshake(ctx context.Context, channel *DirectChannel, p peer) (*connection, Frame, error) {
	limited, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	pub, err := decode(p.PublicKey, 32)
	if err != nil {
		return nil, nil, err
	}
	if err = channel.Establish(limited, s.identity, pub); err != nil {
		return nil, nil, err
	}
	requestID := "hello"
	hello := Frame{"type": "hello", "requestId": requestID, "proto": 1}
	s.mu.Lock()
	if s.server != "" {
		hello["resume"] = Frame{"serverInstanceId": s.server, "cursor": s.cursor}
	}
	s.mu.Unlock()
	if err = channel.SendEncrypted(Frame{"type": "autonomous_device_request", "payload": hello}); err != nil {
		return nil, nil, err
	}
	stop := context.AfterFunc(limited, func() { channel.Close() })
	defer stop()
	for {
		frame, e := channel.ReadDecrypted()
		if e != nil {
			return nil, nil, e
		}
		if stringField(frame, "type") != "autonomous_device_result" {
			continue
		}
		welcome := payloadOf(frame)
		if stringField(welcome, "requestId") != requestID || stringField(welcome, "type") != "hello_result" {
			continue
		}
		if welcome["error"] != nil {
			return nil, nil, fmt.Errorf("Harness capability negotiation: %v", welcome["error"])
		}
		if number(welcome, "proto") != 1 || stringField(welcome, "machineId") != p.MachineID {
			return nil, nil, errors.New("invalid Harness capability response")
		}
		c := &connection{channel: channel, pending: make(map[string]chan response), capabilities: make(map[string]bool)}
		identity := sha256.Sum256(append(append([]byte{}, s.identity.Public().(ed25519.PublicKey)...), pub...))
		c.resultContext = ResultContext{Owner: hex.EncodeToString(identity[:]), MachineID: p.MachineID, ServerInstanceID: stringField(welcome, "serverInstanceId")}
		if caps, ok := welcome["capabilities"].([]any); ok {
			for _, v := range caps {
				cap, _ := v.(string)
				for _, allowed := range capabilities {
					if cap == allowed {
						c.capabilities[cap] = true
					}
				}
			}
		}
		return c, welcome, nil
	}
}
func (c *connection) fail(err error) {
	if err == nil {
		err = errors.New("Harness disconnected")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for id, ch := range c.pending {
		delete(c.pending, id)
		ch <- response{err: err}
	}
}
func (s *Service) readLoop(c *connection) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if c.channel.ws.WriteControl(websocket.PingMessage, nil, time.Now().Add(10*time.Second)) != nil {
					c.channel.Close()
					return
				}
			}
		}
	}()
	for {
		outer, err := c.channel.ReadDecrypted()
		if err != nil {
			return err
		}
		kind := stringField(outer, "type")
		// The CLI seals pair.revoke as an autonomous_device_event payload.
		if kind == "pair.revoke" || stringField(payloadOf(outer), "type") == "pair.revoke" {
			return ErrRevoked
		}
		if kind == "autonomous_device_result" {
			frame := payloadOf(outer)
			id := stringField(frame, "requestId")
			c.mu.Lock()
			ch := c.pending[id]
			if ch != nil {
				delete(c.pending, id)
				ch <- response{frame: frame}
			}
			c.mu.Unlock()
			continue
		}
		if kind != "autonomous_device_event" {
			continue
		}
		frame := payloadOf(outer)
		if s.callbacks.BeforeEvent != nil {
			if err := s.callbacks.BeforeEvent(frame, c.resultContext); err != nil {
				return fmt.Errorf("persist Harness event before replay acknowledgement: %w", err)
			}
		}
		// Never let wire data masquerade as local authenticated provenance.
		frame["_osResultContext"] = c.resultContext
		if !s.emit(frame) {
			return errors.New("Harness event consumer is behind; reconnecting for replay")
		}
		s.mu.Lock()
		if s.conn == c {
			if cursor := number(frame, "eventId"); cursor > s.cursor {
				s.cursor = cursor
			}
		}
		s.mu.Unlock()
	}
}

// StartPair opens the device-local approval window; discovery is provided by the
// OS's existing _autonomous._tcp advertisement, not by an address input here.
func (s *Service) StartPair(ctx context.Context) (PairInfo, error) {
	if err := ctx.Err(); err != nil {
		return PairInfo{}, err
	}
	random, err := randomBytes(6)
	if err != nil {
		return PairInfo{}, err
	}
	const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
	code := make([]byte, 6)
	for i, b := range random {
		code[i] = alphabet[int(b)%len(alphabet)]
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.expireProvisionalLocked(); err != nil {
		return PairInfo{}, err
	}
	if s.disk.Peer != nil {
		return PairInfo{}, errors.New("unpair the current Harness computer before pairing again")
	}
	if s.attempt != nil && s.attempt.info.Pairing {
		return PairInfo{}, errors.New("Harness pairing is already in progress")
	}
	pairCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	deadline, _ := pairCtx.Deadline()
	attempt := &pairAttempt{ctx: pairCtx, cancel: cancel, generation: s.generation, info: PairInfo{Code: string(code), Pairing: true, State: "waiting", ExpiresAt: deadline.UnixMilli()}}
	for i := range code {
		code[i] = 0
	}
	s.attempt = attempt
	s.statusChangedLocked()
	s.status.Pairing = true
	s.status.State = "pairing"
	s.status.Error = ""
	context.AfterFunc(pairCtx, func() { s.finishPair(attempt, pairCtx.Err()) })
	return attempt.info, nil
}
func (s *Service) finishPair(attempt *pairAttempt, err error) {
	s.mu.Lock()
	if s.attempt != attempt || !attempt.info.Pairing {
		s.mu.Unlock()
		return
	}
	attempt.info.Code = ""
	attempt.info.Pairing = false
	s.status.Pairing = false
	if err != nil {
		attempt.info.State = "failed"
		attempt.info.Error = err.Error()
		s.status.Error = err.Error()
		if errors.Is(err, context.DeadlineExceeded) {
			attempt.info.State = "expired"
		}
	} else {
		attempt.info.State = "connecting"
	}
	if s.disk.Peer == nil {
		s.status.State = "unpaired"
	} else if !s.status.Connected {
		s.status.State = "disconnected"
	}
	s.statusChangedLocked()
	s.mu.Unlock()
	attempt.cancel()
}
func (s *Service) Unpair() error {
	_ = s.CancelPair()
	s.mu.Lock()
	s.generation++
	previous := s.disk.Peer
	c := s.conn
	var revoke Frame
	if c != nil && previous != nil {
		revoke = Frame{"type": "pair.revoke", "machineId": previous.MachineID}
	}
	s.disk.Peer = nil
	err := s.saveLocked()
	if err != nil {
		s.disk.Peer = previous
	}
	s.conn = nil
	s.status = Status{State: "unpaired", Capabilities: []string{}}
	s.statusChangedLocked()
	if err != nil {
		s.status.Error = "Remove Harness trust: " + err.Error()
		if s.disk.Peer != nil {
			s.status.State = "disconnected"
		}
	}
	s.server = ""
	s.cursor = 0
	sockets := make([]*DirectChannel, 0, len(s.sockets))
	for socket := range s.sockets {
		sockets = append(sockets, socket)
	}
	s.mu.Unlock()
	// Never let a stalled/offline CLI delay local trust removal or the MQTT
	// acknowledgement. The socket is closed below; this best-effort notice is
	// only for promptly clearing the CLI's credentials when it is reachable.
	if revoke != nil {
		go func() { _ = c.channel.SendEncrypted(revoke) }()
	}
	for _, socket := range sockets {
		socket.Close()
	}
	if c != nil {
		c.fail(errors.New("Harness unpaired"))
	}
	return err
}
func (s *Service) Start(ctx context.Context) {
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return
	}
	s.started = true
	s.ctx = ctx
	s.mu.Unlock()
	go func() {
		for {
			select {
			case <-ctx.Done():
				_ = s.CancelPair()
				s.mu.Lock()
				for socket := range s.sockets {
					socket.Close()
				}
				s.mu.Unlock()
				return
			case frame := <-s.events:
				if s.callbacks.OnEvent != nil {
					s.callbacks.OnEvent(frame)
				}
			}
		}
	}()
}
func (s *Service) emit(frame Frame) bool {
	select {
	case s.events <- frame:
		return true
	default:
		return false
	}
}

// ServeHTTP is mounted on the OS's existing HTTP server. No additional port is
// opened. Pairing authenticates new computers; pinned E2EE authenticates reconnects.
func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/api/harness/ws" || r.URL.RawQuery != "" || r.Header.Get("Origin") != "" {
		http.Error(w, "invalid Harness connection", http.StatusForbidden)
		return
	}
	s.mu.Lock()
	if s.incoming >= 4 || s.ctx.Err() != nil {
		s.mu.Unlock()
		http.Error(w, "Harness unavailable", http.StatusServiceUnavailable)
		return
	}
	s.incoming++
	lifetime := s.ctx
	s.mu.Unlock()
	defer func() { s.mu.Lock(); s.incoming--; s.mu.Unlock() }()
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return r.Header.Get("Origin") == "" }}
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer ws.Close()
	ws.SetReadLimit(1024 * 1024)
	channel := &DirectChannel{ws: ws}
	s.mu.Lock()
	s.sockets[channel] = true
	s.mu.Unlock()
	defer func() { s.mu.Lock(); delete(s.sockets, channel); s.mu.Unlock() }()
	stop := context.AfterFunc(lifetime, func() { ws.Close() })
	defer stop()
	_ = ws.SetReadDeadline(time.Now().Add(10 * time.Second))
	metadata, err := channel.Read()
	if err != nil {
		return
	}
	payload := payloadOf(metadata)
	machineID := stringField(payload, "machineId")
	label := stringField(payload, "label")
	if stringField(metadata, "type") != "machine_select" || machineID == "" || len(machineID) > 200 || len(label) > 200 {
		return
	}
	channel.machineID = machineID
	s.mu.Lock()
	if err = s.expireProvisionalLocked(); err != nil {
		s.mu.Unlock()
		return
	}
	attempt := s.attempt
	var pinned peer
	hasPin := s.disk.Peer != nil
	if hasPin {
		pinned = *s.disk.Peer
	}
	pairing := !hasPin && attempt != nil && attempt.info.Pairing && attempt.ctx.Err() == nil && attempt.channel == nil
	if pairing {
		attempt.channel = channel
		attempt.info.MachineID = machineID
	}
	generation := s.generation
	s.mu.Unlock()
	if (!hasPin && !pairing) || (hasPin && pinned.MachineID != machineID) {
		return
	}
	if hasPin && pinned.Protocol != "harness-device-direct-v1" && pinned.Protocol != "harness-device-relay-v1" {
		return
	}
	if err = channel.Write(Frame{"type": "machine_selected", "payload": Frame{"machineId": machineID}}); err != nil {
		return
	}
	_ = ws.SetReadDeadline(time.Time{})
	if pairing {
		s.mu.Lock()
		code := attempt.info.Code
		s.mu.Unlock()
		err = channel.Pair(attempt.ctx, s.identity, code, func(pub []byte) error {
			s.mu.Lock()
			defer s.mu.Unlock()
			if s.attempt != attempt || !attempt.info.Pairing || attempt.ctx.Err() != nil || s.generation != attempt.generation {
				return errors.New("pairing cancelled")
			}
			previous := s.disk.Peer
			s.disk.Peer = &peer{Protocol: "harness-device-direct-v1", MachineID: machineID, MachineName: label, PublicKey: b64(pub), ProvisionalUntil: time.Now().Add(5 * time.Minute).UnixMilli()}
			if e := s.saveLocked(); e != nil {
				s.disk.Peer = previous
				return e
			}
			s.generation++
			attempt.generation = s.generation
			attempt.provisional = true
			return nil
		})
		code = ""
		s.finishPair(attempt, err)
		if err != nil {
			return
		}
		s.mu.Lock()
		if s.disk.Peer == nil {
			s.mu.Unlock()
			return
		}
		pinned = *s.disk.Peer
		generation = s.generation
		s.mu.Unlock()
	}
	connection, welcome, err := s.handshake(lifetime, channel, pinned)
	if err != nil {
		if errors.Is(err, ErrRevoked) {
			s.revokedByComputer(generation)
		}
		return
	}
	s.mu.Lock()
	if generation != s.generation || s.disk.Peer == nil || s.disk.Peer.PublicKey != pinned.PublicKey {
		s.mu.Unlock()
		return
	}
	old := s.conn
	priorPeer := *s.disk.Peer
	s.conn = connection
	s.disk.Peer.ProvisionalUntil = 0
	s.disk.Peer.Protocol = "harness-device-direct-v1"
	if err = s.saveLocked(); err != nil {
		*s.disk.Peer = priorPeer
		s.status.Error = "Confirm Harness trust: " + err.Error()
		s.conn = old
		s.mu.Unlock()
		return
	}
	s.status.Connected = true
	s.status.Pairing = false
	s.status.State = "connected"
	s.status.Error = ""
	s.status.ServerInstanceID = stringField(welcome, "serverInstanceId")
	s.status.Capabilities = nil
	for _, cap := range capabilities {
		if connection.capabilities[cap] {
			s.status.Capabilities = append(s.status.Capabilities, cap)
		}
	}
	if s.attempt != nil && s.attempt.provisional && s.attempt.info.MachineID == machineID {
		s.attempt.info.State = "paired"
	}
	s.statusChangedLocked()
	if s.server != stringField(welcome, "serverInstanceId") || welcome["resumed"] != true {
		s.cursor = number(welcome, "cursor")
	}
	s.server = stringField(welcome, "serverInstanceId")
	s.mu.Unlock()
	if old != nil {
		old.channel.Close()
		old.fail(errors.New("Harness session replaced by authenticated reconnect"))
	}
	_ = ws.SetReadDeadline(time.Now().Add(60 * time.Second))
	ws.SetPongHandler(func(string) error { return ws.SetReadDeadline(time.Now().Add(60 * time.Second)) })
	err = s.readLoop(connection)
	connection.fail(err)
	s.mu.Lock()
	if s.conn == connection {
		s.conn = nil
		s.status.Connected = false
		s.status.State = "disconnected"
		s.statusChangedLocked()
	}
	s.mu.Unlock()
	if errors.Is(err, ErrRevoked) {
		s.revokedByComputer(generation)
	}
}

// revokedByComputer removes the pin after the paired computer rejected or
// revoked this device. generation guards against a local unpair/re-pair that
// raced with the revoke; s.conn is already nil here so Unpair sends no echo.
func (s *Service) revokedByComputer(generation uint64) {
	s.mu.Lock()
	stale := s.generation != generation || s.disk.Peer == nil
	s.mu.Unlock()
	if stale {
		return
	}
	if err := s.Unpair(); err != nil {
		return
	}
	if s.callbacks.OnRevoked != nil {
		s.callbacks.OnRevoked()
	}
}
