package harness

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type VoiceTransport interface {
	Status() Status
	Request(context.Context, Frame) (Frame, error)
}
type VoiceCallbacks struct {
	// SupportsMode checks the configured physical input; absent callbacks fail closed.
	SupportsMode func(context.Context) (bool, error)
	OnDispatch   func(agentID, runID string)
	// OnDispatchRequest registers delivery correlation before input can reach
	// the remote engine. When provided it takes precedence over OnDispatch.
	OnDispatchRequest func(agentID, runID string, frame Frame)
	OnResponse        func(agentID, runID, text string)
}
type VoicePending struct {
	IdempotencyKey string `json:"idempotencyKey"`
	AgentID        string `json:"agentId"`
	MachineID      string `json:"machineId"`
	RunID          string `json:"runId"`
}
type VoiceModeState struct {
	Supported      bool          `json:"supported"`
	Enabled        bool          `json:"enabled"`
	Generation     uint64        `json:"generation"`
	MachineID      string        `json:"machineId"`
	AgentID        string        `json:"agentId"`
	AgentName      string        `json:"agentName,omitempty"`
	FocusRevision  string        `json:"focusRevision"`
	FocusAvailable bool          `json:"focusAvailable"`
	Pending        *VoicePending `json:"pending,omitempty"`
	Error          string        `json:"error,omitempty"`
}
type VoiceQuestion struct {
	Key     string   `json:"key"`
	Q       string   `json:"q"`
	Options []string `json:"options"`
	Multi   bool     `json:"multi"`
}
type voiceQuestionSet struct {
	RequestID string          `json:"requestId"`
	Questions []VoiceQuestion `json:"questions"`
}

// voiceOperationLock serializes wire operations, not remote engine turns.
type voiceOperationLock struct {
	once  sync.Once
	token chan struct{}
}

func (l *voiceOperationLock) init() { l.once.Do(func() { l.token = make(chan struct{}, 1) }) }
func (l *voiceOperationLock) TryLock() bool {
	l.init()
	select {
	case l.token <- struct{}{}:
		return true
	default:
		return false
	}
}
func (l *voiceOperationLock) Lock(ctx context.Context) error {
	l.init()
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case l.token <- struct{}{}:
		if err := ctx.Err(); err != nil {
			l.Unlock()
			return err
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (l *voiceOperationLock) Unlock() { <-l.token }

type VoiceController struct {
	mu         sync.Mutex
	op         voiceOperationLock
	focusMu    sync.Mutex
	focusWake  chan struct{}
	startOnce  sync.Once
	modeIntent uint64
	gestures   voiceGestures
	transport  VoiceTransport
	callbacks  VoiceCallbacks
	state      VoiceModeState
	seen       map[string]bool
	order      []string
	pending    []*VoicePending
	question   *voiceQuestionSet
	answers    map[string]string
}

var voiceGeneration atomic.Uint64

func nextVoiceGeneration() uint64 {
	seed := uint64(time.Now().UnixMicro())
	for {
		old := voiceGeneration.Load()
		if seed <= old {
			seed = old + 1
		}
		if voiceGeneration.CompareAndSwap(old, seed) {
			break
		}
	}
	return seed
}

func NewVoiceController(t VoiceTransport, cb VoiceCallbacks) *VoiceController {
	return &VoiceController{transport: t, callbacks: cb, state: VoiceModeState{Generation: nextVoiceGeneration()}, seen: make(map[string]bool), focusWake: make(chan struct{}, 1)}
}
func (v *VoiceController) State() VoiceModeState {
	v.mu.Lock()
	defer v.mu.Unlock()
	s := v.state
	if s.Pending != nil {
		p := *s.Pending
		s.Pending = &p
	}
	return s
}
func (v *VoiceController) connected(machine string) error {
	s := v.transport.Status()
	if !s.Paired || !s.Connected {
		return errors.New("Harness is offline")
	}
	if machine != "" && s.MachineID != machine {
		return errors.New("Harness paired computer changed")
	}
	return nil
}
func voiceError(f Frame) error {
	if e, ok := f["error"]; ok && e != nil {
		return fmt.Errorf("Harness: %v", e)
	}
	return nil
}
func (v *VoiceController) request(ctx context.Context, f Frame) (Frame, error) {
	r, e := v.transport.Request(ctx, f)
	if e == nil {
		e = voiceError(r)
	}
	return r, e
}
func (v *VoiceController) Agents(ctx context.Context) (Frame, error) {
	if e := v.connected(""); e != nil {
		return nil, e
	}
	return v.request(ctx, Frame{"type": "agents.list"})
}

// Start keeps the read-only focus mirror current even when voice mode is off.
func (v *VoiceController) Start(ctx context.Context) {
	v.startOnce.Do(func() {
		go func() {
			ticker := time.NewTicker(2 * time.Second)
			defer ticker.Stop()
			for {
				_ = v.RefreshSupport(ctx)
				_ = v.RefreshFocus(ctx)
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
				case <-v.focusWake:
				}
			}
		}()
	})
}
func (v *VoiceController) NotifyFocusChanged() {
	select {
	case v.focusWake <- struct{}{}:
	default:
	}
}
func (v *VoiceController) RefreshFocus(ctx context.Context) error {
	v.focusMu.Lock()
	defer v.focusMu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	status := v.transport.Status()
	var snapshot struct {
		Focus *struct {
			MachineID string `json:"machineId"`
			AgentID   string `json:"agentId"`
			Name      string `json:"name"`
		} `json:"focus"`
		Revision string `json:"focusRevision"`
	}
	var err error
	supported := false
	for _, cap := range status.Capabilities {
		if cap == "focus.get" {
			supported = true
		}
	}
	if !status.Paired || !status.Connected {
		err = errors.New("Harness is offline")
	} else if !supported {
		err = errors.New("Update Harness CLI to support app focus (focus.get)")
	} else {
		var frame Frame
		frame, err = v.request(ctx, Frame{"type": "focus.get"})
		if err == nil {
			var raw []byte
			raw, err = json.Marshal(frame)
			if err == nil {
				err = json.Unmarshal(raw, &snapshot)
			}
			if err == nil && snapshot.Revision == "" {
				err = errors.New("Harness focus revision is missing")
			}
			if err == nil {
				err = v.connected(status.MachineID)
			}
		}
	}
	machine, agent, name, revision := "", "", "", ""
	available := false
	if err == nil {
		revision = snapshot.Revision
		if snapshot.Focus == nil {
			err = errors.New("Focus an agent in the Harness app")
		} else {
			machine, agent, name = snapshot.Focus.MachineID, snapshot.Focus.AgentID, snapshot.Focus.Name
			if machine != status.MachineID {
				err = errors.New("The focused agent is on another computer; focus an agent on the paired computer")
			} else if agent == "" {
				err = errors.New("Focus an agent in the Harness app")
			} else {
				available = true
			}
		}
	}
	v.setFocus(machine, agent, name, revision, available, err)
	return err
}

// setFocus applies one focus snapshot to the shared state. err is the reason
// focus is unavailable (nil when available).
func (v *VoiceController) setFocus(machine, agent, name, revision string, available bool, err error) {
	v.mu.Lock()
	changed := v.state.FocusRevision != revision || v.state.MachineID != machine || v.state.AgentID != agent || v.state.FocusAvailable != available
	if changed {
		// Off-mode focus polling must not invalidate ordinary HAL voice captures.
		if v.state.Enabled {
			v.state.Generation = nextVoiceGeneration()
		}
		v.question, v.answers = nil, nil
	}
	v.state.MachineID, v.state.AgentID, v.state.AgentName = machine, agent, name
	v.state.FocusRevision, v.state.FocusAvailable = revision, available
	if v.state.Pending == nil {
		v.state.Error = ""
		if err != nil {
			v.state.Error = err.Error()
		}
	}
	v.mu.Unlock()
}

// RefreshSupport performs I/O outside the state mutex. State remains a cheap snapshot.
func (v *VoiceController) RefreshSupport(ctx context.Context) error {
	supported := false
	var err error
	if v.callbacks.SupportsMode != nil {
		ctx, cancel := context.WithTimeout(ctx, time.Second)
		defer cancel()
		supported, err = v.callbacks.SupportsMode(ctx)
	}
	supported = supported && err == nil
	v.mu.Lock()
	v.state.Supported = supported
	if !supported && v.state.Enabled {
		v.setModeLocked(false)
	}
	v.mu.Unlock()
	if err != nil {
		return fmt.Errorf("Cannot verify MPR121 support: %w", err)
	}
	if !supported {
		return errors.New("Harness-only mode requires an enabled MPR121 input")
	}
	return nil
}
func (v *VoiceController) SetMode(ctx context.Context, enabled bool) (VoiceModeState, error) {
	v.mu.Lock()
	intent := v.modeIntent
	v.mu.Unlock()
	if enabled {
		if err := v.RefreshSupport(ctx); err != nil {
			return v.State(), err
		}
	}
	v.mu.Lock()
	if enabled && (v.modeIntent != intent || !v.state.Supported || ctx.Err() != nil) {
		v.mu.Unlock()
		return v.State(), errors.New("Voice mode changed while checking MPR121 support")
	}
	v.setModeLocked(enabled)
	v.mu.Unlock()
	v.NotifyFocusChanged()
	return v.State(), nil
}

func (v *VoiceController) setModeLocked(enabled bool) {
	v.modeIntent++
	if v.state.Enabled != enabled {
		v.state.Generation = nextVoiceGeneration()
		v.question, v.answers = nil, nil
	}
	v.state.Enabled = enabled
}
func (v *VoiceController) checkFocus(ctx context.Context, s VoiceModeState) error {
	if err := v.RefreshFocus(ctx); err != nil {
		return err
	}
	current := v.State()
	if !current.Enabled || current.Generation != s.Generation || current.FocusRevision != s.FocusRevision {
		return errors.New("Harness app focus or voice mode changed; please repeat")
	}
	return nil
}
func (v *VoiceController) liveQuestion(ctx context.Context, s VoiceModeState) (*voiceQuestionSet, error) {
	if e := v.connected(s.MachineID); e != nil {
		return nil, e
	}
	f, e := v.request(ctx, Frame{"type": "status", "machineId": s.MachineID, "agentId": s.AgentID})
	if e != nil {
		return nil, e
	}
	if f["openQuestion"] == nil {
		return nil, nil
	}
	raw, e := json.Marshal(f["openQuestion"])
	if e != nil {
		return nil, e
	}
	var q voiceQuestionSet
	if e = json.Unmarshal(raw, &q); e != nil {
		return nil, fmt.Errorf("decode Harness question: %w", e)
	}
	if q.RequestID == "" || len(q.Questions) == 0 {
		return nil, errors.New("Harness question is malformed")
	}
	keys := map[string]bool{}
	for _, row := range q.Questions {
		if row.Key == "" || keys[row.Key] {
			return nil, errors.New("Harness question keys are malformed")
		}
		keys[row.Key] = true
	}
	return &q, nil
}
func (v *VoiceController) Question(ctx context.Context) (Frame, error) {
	s := v.State()
	if !s.Enabled {
		return Frame{"question": nil}, nil
	}
	if e := v.RefreshFocus(ctx); e != nil {
		return nil, e
	}
	s = v.State()
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return nil, e
	}
	if q == nil {
		return Frame{"question": nil}, nil
	}
	if current := v.State(); current.Generation != s.Generation {
		return nil, errors.New("Harness app focus changed; refresh the question")
	}
	return Frame{"agentId": s.AgentID, "focusRevision": s.FocusRevision, "questionRequestId": q.RequestID, "questions": q.Questions}, nil
}
func (v *VoiceController) begin(runID string, generation uint64) (VoiceModeState, bool, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	s := v.state
	if strings.TrimSpace(runID) == "" {
		return s, false, errors.New("Harness voice run ID is required")
	}
	if v.seen[runID] {
		return s, true, nil
	}
	v.seen[runID] = true
	v.order = append(v.order, runID)
	if len(v.order) > 1024 {
		delete(v.seen, v.order[0])
		v.order = v.order[1:]
	}
	if !s.Enabled {
		return s, false, errors.New("Harness-only mode is off")
	}
	if generation != s.Generation {
		return s, false, errors.New("Harness voice mode changed; please repeat")
	}
	return s, false, nil
}
func (v *VoiceController) ready(ctx context.Context, s VoiceModeState) error {
	if e := v.checkFocus(ctx, s); e != nil {
		return e
	}
	if e := v.connected(s.MachineID); e != nil {
		return e
	}
	// Earlier uncertain deliveries remain inspectable without blocking a new turn.
	return nil
}
func (v *VoiceController) Submit(ctx context.Context, text, runID string, generation uint64) error {
	if err := v.op.Lock(ctx); err != nil {
		return err
	}
	defer v.op.Unlock()
	s, duplicate, e := v.begin(runID, generation)
	if duplicate || e != nil {
		return e
	}

	text = strings.TrimSpace(text)
	if text == "" || len(text) > 16384 {
		return errors.New("Voice text must contain 1 to 16384 UTF-8 bytes")
	}
	if e = v.ready(ctx, s); e != nil {
		return e
	}
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return e
	}
	if e = v.checkFocus(ctx, s); e != nil {
		return e
	}
	v.mu.Lock()
	if v.state.Generation != s.Generation || !v.state.Enabled {
		v.mu.Unlock()
		return errors.New("Harness voice mode changed; please repeat")
	}
	if v.question != nil && !reflect.DeepEqual(v.question, q) {
		v.question = nil
		v.answers = nil
		v.mu.Unlock()
		return errors.New("Harness question changed; refresh the question and repeat your answer")
	}
	if q == nil {
		v.mu.Unlock()
		return v.dispatch(ctx, s, runID, Frame{"type": "turn.send", "text": text})
	}
	if v.question == nil {
		v.question = q
		v.answers = make(map[string]string)
	}
	for _, row := range q.Questions {
		if _, ok := v.answers[row.Key]; !ok {
			v.answers[row.Key] = text
			break
		}
	}
	var next *VoiceQuestion
	for i := range q.Questions {
		if _, ok := v.answers[q.Questions[i].Key]; !ok {
			next = &q.Questions[i]
			break
		}
	}
	answers := make(map[string]string, len(v.answers))
	for k, a := range v.answers {
		answers[k] = a
	}
	v.mu.Unlock()
	if next != nil {
		if v.callbacks.OnResponse != nil {
			prompt := next.Q
			if len(next.Options) > 0 {
				prompt += "\n" + strings.Join(next.Options, ", ")
			}
			v.callbacks.OnResponse(s.AgentID, runID, prompt)
		}
		return nil
	}
	return v.dispatch(ctx, s, runID, Frame{"type": "question.answer", "questionRequestId": q.RequestID, "answers": answers})
}
func (v *VoiceController) Answer(ctx context.Context, questionID string, answers map[string]string, runID string, focusRevision string) error {
	if err := v.op.Lock(ctx); err != nil {
		return err
	}
	defer v.op.Unlock()
	s, duplicate, e := v.begin(runID, v.State().Generation)
	if duplicate || e != nil {
		return e
	}
	if focusRevision == "" || focusRevision != s.FocusRevision {
		return errors.New("Harness app focus changed; refresh the question")
	}

	if e = v.ready(ctx, s); e != nil {
		return e
	}
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return e
	}
	if q == nil || q.RequestID != questionID {
		return errors.New("Harness question is no longer open")
	}
	if len(answers) != len(q.Questions) {
		return errors.New("Answer every Harness question using its exact key")
	}
	copyAnswers := map[string]string{}
	for _, row := range q.Questions {
		a, ok := answers[row.Key]
		if !ok || strings.TrimSpace(a) == "" {
			return errors.New("Answer every Harness question using its exact key")
		}
		copyAnswers[row.Key] = a
	}
	if e = v.checkFocus(ctx, s); e != nil {
		return e
	}
	return v.dispatch(ctx, s, runID, Frame{"type": "question.answer", "questionRequestId": questionID, "answers": copyAnswers})
}
func knownVoiceReceipt(f Frame) bool {
	raw, _ := json.Marshal(f["receipt"])
	var r struct {
		State string `json:"state"`
	}
	_ = json.Unmarshal(raw, &r)
	switch r.State {
	case "queued", "delivered", "started", "completed", "rejected":
		return true
	}
	return false
}
func (v *VoiceController) dispatch(ctx context.Context, s VoiceModeState, runID string, f Frame) error {
	if e := v.connected(s.MachineID); e != nil {
		return e
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d:%s:%s:%s", s.Generation, s.MachineID, s.AgentID, runID)))
	// CLI keys allow at most 64 characters. The 128-bit digest retains ample
	// collision resistance while including the readable operation prefix.
	key := "voice-" + hex.EncodeToString(sum[:16])
	p := &VoicePending{IdempotencyKey: key, AgentID: s.AgentID, MachineID: s.MachineID, RunID: runID}
	v.mu.Lock()
	if !v.state.Enabled || v.state.Generation != s.Generation {
		v.mu.Unlock()
		return errors.New("Harness voice mode changed; please repeat")
	}
	if len(v.pending) >= 64 {
		v.mu.Unlock()
		return errors.New("Too many unconfirmed Harness deliveries; inspect or resolve their receipts")
	}
	v.pending = append(v.pending, p)
	v.state.Pending = v.pending[0]
	v.question = nil
	v.answers = nil
	v.mu.Unlock()
	f["machineId"] = s.MachineID
	f["agentId"] = s.AgentID
	f["idempotencyKey"] = key
	f["focusRevision"] = s.FocusRevision
	if v.callbacks.OnDispatchRequest != nil {
		v.callbacks.OnDispatchRequest(s.AgentID, runID, f)
	} else if v.callbacks.OnDispatch != nil {
		v.callbacks.OnDispatch(s.AgentID, runID)
	}
	result, e := v.transport.Request(ctx, f)
	known := knownVoiceReceipt(result)
	if known && e == nil {
		raw, _ := json.Marshal(result["receipt"])
		var receipt struct {
			State string `json:"state"`
			Error any    `json:"error"`
		}
		_ = json.Unmarshal(raw, &receipt)
		if receipt.State == "rejected" {
			e = fmt.Errorf("Harness rejected the request: %v", receipt.Error)
		}
	}
	var unknown *DeliveryUnknownError
	// The transport marks every failure after sending as DeliveryUnknownError.
	if e != nil && !errors.As(e, &unknown) {
		known = true
	}
	if e == nil {
		e = voiceError(result)
		if e != nil {
			raw, _ := json.Marshal(result["error"])
			var remote struct {
				Code string `json:"code"`
			}
			_ = json.Unmarshal(raw, &remote)
			switch remote.Code {
			case "FOCUS_CHANGED", "INVALID_REQUEST", "UNSUPPORTED_CAPABILITY", "MISSING_TARGET", "MACHINE_MISMATCH", "AGENT_NOT_FOUND", "QUESTION_STALE", "PAYLOAD_TOO_LARGE", "RATE_LIMITED", "BACKPRESSURE":
				known = true
			}
		}
	}
	v.mu.Lock()
	if known {
		v.removePendingLocked(key)
	} else {
		v.state.Error = "Harness delivery is unconfirmed; inspect the receipt before sending again"
	}
	v.mu.Unlock()
	if !known {
		if e == nil {
			e = errors.New("Harness delivery is unconfirmed; inspect the receipt before sending again")
		}
		return &DeliveryUnknownError{RequestID: key, Cause: e}
	}
	if e != nil {
		return e
	}
	return nil
}
func (v *VoiceController) receipt(ctx context.Context) (Frame, error) {
	s := v.State()
	if s.Pending == nil {
		return Frame{"receipt": nil, "pending": false}, nil
	}
	if e := v.connected(s.Pending.MachineID); e != nil {
		return nil, e
	}
	f, e := v.request(ctx, Frame{"type": "receipt.get", "idempotencyKey": s.Pending.IdempotencyKey})
	if e != nil {
		return nil, e
	}
	if knownVoiceReceipt(f) {
		v.mu.Lock()
		if v.state.Pending != nil && v.state.Pending.IdempotencyKey == s.Pending.IdempotencyKey {
			v.removePendingLocked(s.Pending.IdempotencyKey)
		}
		v.mu.Unlock()
	}
	return f, nil
}
func (v *VoiceController) Receipt(ctx context.Context) (Frame, error) {
	if !v.op.TryLock() {
		return nil, errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	return v.receipt(ctx)
}
func (v *VoiceController) Resolve(resolution, key string) error {
	if !v.op.TryLock() {
		return errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	v.mu.Lock()
	defer v.mu.Unlock()
	if resolution != "do_not_retry" || v.state.Pending == nil || v.state.Pending.IdempotencyKey != key {
		return errors.New("Resolve requires do_not_retry and the current pending idempotency key")
	}
	v.removePendingLocked(key)
	return nil
}

// removePendingLocked removes only the reconciled delivery and exposes the oldest.
func (v *VoiceController) removePendingLocked(key string) {
	for i, p := range v.pending {
		if p.IdempotencyKey == key {
			v.pending = append(v.pending[:i], v.pending[i+1:]...)
			break
		}
	}
	v.state.Pending = nil
	v.state.Error = ""
	if len(v.pending) > 0 {
		v.state.Pending = v.pending[0]
		v.state.Error = "Harness delivery is unconfirmed; inspect the receipt before sending again"
	}
}
