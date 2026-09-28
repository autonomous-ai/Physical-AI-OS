package harness

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// ResultInput is a durable reservation made before sending an input. Owner is
// the authenticated local device/trust identity, never a value from an event.
type ResultInput struct {
	Owner            string    `json:"owner"`
	ServerInstanceID string    `json:"serverInstanceId"`
	AgentID          string    `json:"agentId"`
	IdempotencyKey   string    `json:"idempotencyKey"`
	DeliveryID       string    `json:"deliveryId,omitempty"`
	RunID            string    `json:"runId"`
	Text             string    `json:"text,omitempty"`
	Channel          string    `json:"channel"`
	Destination      string    `json:"destination"`
	ExpiresAt        time.Time `json:"expiresAt"`
	ResultID         string    `json:"resultId,omitempty"`
}
type ResultMember struct {
	DeliveryID     string `json:"deliveryId"`
	IdempotencyKey string `json:"idempotencyKey"`
}
type ResultCorrelation struct {
	Scope        string         `json:"scope"`
	Inputs       []ResultMember `json:"inputs"`
	EngineTurnID string         `json:"engineTurnId,omitempty"`
}
type ResultPayload struct {
	ServerInstanceID string            `json:"serverInstanceId"`
	ResultID         string            `json:"resultId"`
	Correlation      ResultCorrelation `json:"correlation"`
	Outcome          string            `json:"outcome"`
	FullText         string            `json:"fullText"`
}
type ResultRecord struct {
	Owner       string        `json:"owner"`
	AgentID     string        `json:"agentId"`
	Payload     ResultPayload `json:"payload"`
	Inputs      []ResultInput `json:"inputs"`
	SpeechState string        `json:"speechState"`
	Channel     string        `json:"channel"`
	Destination string        `json:"destination"`
}
type StagedResult struct {
	Owner string `json:"owner"`
	Frame Frame  `json:"frame"`
}
type ResultQuestion struct {
	Owner            string `json:"owner"`
	ServerInstanceID string `json:"serverInstanceId"`
	AgentID          string `json:"agentId"`
	QuestionID       string `json:"questionId"`
	TaskKey          string `json:"taskKey"`
}
type ResultAnswer struct {
	Input        ResultInput `json:"input"`
	QuestionID   string      `json:"questionId"`
	Parent       ResultInput `json:"parent"`
	ReceiptState string      `json:"receiptState"`
}
type resultDisk struct {
	Version   int                       `json:"version"`
	Inputs    map[string]ResultInput    `json:"inputs"`
	Results   map[string]ResultRecord   `json:"results"`
	Staged    map[string]StagedResult   `json:"staged,omitempty"`
	Questions map[string]ResultQuestion `json:"questions,omitempty"`
	Answers   map[string]ResultAnswer   `json:"answers,omitempty"`
}
type ResultStore struct {
	mu   sync.Mutex
	path string
	data resultDisk
}

const resultFileLimit = 64 << 20
const resultRecordLimit = 4096

func resultIdentity(parts ...string) string { b, _ := json.Marshal(parts); return string(b) }
func resultToken(s string) bool {
	return strings.TrimSpace(s) != "" && len(s) <= 4096 && !strings.ContainsAny(s, "\x00\r\n")
}

// ParseSummaryResult validates correlated results on the existing summary event.
// Limits are local admission limits, not changes to the producer wire contract.
func ParseSummaryResult(frame Frame) (string, ResultPayload, error) {
	var p ResultPayload
	bad := func() (string, ResultPayload, error) { return "", p, fmt.Errorf("invalid correlated turn.summary") }
	if stringField(frame, "type") != "event" || stringField(frame, "kind") != "turn.summary" || !resultToken(stringField(frame, "agentId")) {
		return bad()
	}
	payload := payloadOf(frame)
	copyPayload := Frame{}
	for k, v := range payload {
		copyPayload[k] = v
	}
	// Existing singular metadata cannot nominate one member of a group. For a
	// single input the original key must agree; turnId is engine metadata only.
	delete(copyPayload, "idempotencyKey")
	delete(copyPayload, "turnId")
	b, err := json.Marshal(copyPayload)
	if err != nil || len(b) > 512<<10 {
		return bad()
	}
	// Legacy summary fields remain compatible but do not enter result identity.
	dec := json.NewDecoder(bytes.NewReader(b))
	if err = dec.Decode(&p); err != nil {
		return bad()
	}
	if !resultToken(p.ServerInstanceID) || !resultToken(p.ResultID) || strings.TrimSpace(p.FullText) == "" || len(p.FullText) > 256<<10 {
		return bad()
	}
	if p.Outcome != "completed" && p.Outcome != "failed" && p.Outcome != "cancelled" {
		return bad()
	}
	n := len(p.Correlation.Inputs)
	if n < 1 || n > 64 || (p.Correlation.Scope != "input" && p.Correlation.Scope != "group") || (p.Correlation.Scope == "input" && n != 1) || (p.Correlation.Scope == "group" && n < 2) {
		return bad()
	}
	if len(p.Correlation.EngineTurnID) > 4096 {
		return bad()
	}
	keys, deliveries := map[string]bool{}, map[string]bool{}
	for _, m := range p.Correlation.Inputs {
		if !resultToken(m.IdempotencyKey) || !resultToken(m.DeliveryID) || keys[m.IdempotencyKey] || deliveries[m.DeliveryID] {
			return bad()
		}
		keys[m.IdempotencyKey] = true
		deliveries[m.DeliveryID] = true
	}
	for _, fields := range []Frame{frame, payload} {
		for _, key := range []string{"idempotencyKey", "turnId", "runId", "run_id"} {
			if value, ok := fields[key]; ok {
				if p.Correlation.Scope == "group" {
					return bad()
				}
				if key == "turnId" && value == nil {
					continue
				} // Existing single-input turnId is nullable.
				text, valid := value.(string)
				if !valid || !resultToken(text) {
					return bad()
				}
				if key == "idempotencyKey" && text != p.Correlation.Inputs[0].IdempotencyKey {
					return bad()
				}
			}
		}
	}
	// Membership is a set; reordered replay is identical.
	sort.Slice(p.Correlation.Inputs, func(i, j int) bool {
		return p.Correlation.Inputs[i].IdempotencyKey < p.Correlation.Inputs[j].IdempotencyKey
	})
	return stringField(frame, "agentId"), p, nil
}

func checkResultPath(path string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return fmt.Errorf("result store requires clean absolute path")
	}
	for cur := path; ; cur = filepath.Dir(cur) {
		fi, err := os.Lstat(cur)
		if err == nil {
			if fi.Mode()&os.ModeSymlink != 0 {
				return fmt.Errorf("result store refuses symlink: %s", cur)
			}
			if cur == path && !fi.Mode().IsRegular() {
				return fmt.Errorf("result store is not a regular file")
			}
			if cur != path && !fi.IsDir() {
				return fmt.Errorf("result store parent is not a directory")
			}
		} else if !os.IsNotExist(err) {
			return err
		}
		if cur == filepath.Dir(cur) {
			break
		}
	}
	return nil
}
func OpenResultStore(path string) (*ResultStore, error) {
	if err := checkResultPath(path); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	s := &ResultStore{path: path, data: resultDisk{Version: 1, Inputs: map[string]ResultInput{}, Results: map[string]ResultRecord{}}}
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, resultFileLimit+1))
	if err != nil || len(b) > resultFileLimit {
		return nil, fmt.Errorf("read result store: size/read failure")
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err = dec.Decode(&s.data); err != nil {
		return nil, fmt.Errorf("decode result store: %w", err)
	}
	if err = dec.Decode(new(any)); err != io.EOF {
		return nil, fmt.Errorf("trailing result store data")
	}
	if s.data.Version != 1 || s.data.Inputs == nil || s.data.Results == nil || len(s.data.Inputs) > resultRecordLimit || len(s.data.Results) > resultRecordLimit {
		return nil, fmt.Errorf("invalid result store")
	}
	if s.data.Staged == nil {
		s.data.Staged = map[string]StagedResult{}
	}
	if len(s.data.Staged) > resultRecordLimit {
		return nil, fmt.Errorf("staged result capacity exceeded")
	}
	if len(s.data.Questions) > resultRecordLimit || len(s.data.Answers) > resultRecordLimit {
		return nil, fmt.Errorf("question/answer capacity exceeded")
	}
	changed := false
	for k, r := range s.data.Results {
		if r.SpeechState == "claimed" {
			r.SpeechState = "uncertain"
			s.data.Results[k] = r
			changed = true
		}
	}
	if changed {
		if err = s.save(s.data); err != nil {
			return nil, err
		}
	}
	return s, nil
}
func (s *ResultStore) save(d resultDisk) error {
	if err := checkResultPath(s.path); err != nil {
		return err
	}
	b, err := json.Marshal(d)
	if err != nil {
		return err
	}
	if len(b) > resultFileLimit {
		return fmt.Errorf("result store capacity exceeded")
	}
	f, err := os.CreateTemp(filepath.Dir(s.path), ".results-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(f.Name(), s.path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(s.path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
func cloneResultData(d resultDisk) resultDisk {
	b, _ := json.Marshal(d)
	var c resultDisk
	_ = json.Unmarshal(b, &c)
	return c
}
func cloneResult(r ResultRecord) ResultRecord {
	r.Inputs = append([]ResultInput(nil), r.Inputs...)
	r.Payload.Correlation.Inputs = append([]ResultMember(nil), r.Payload.Correlation.Inputs...)
	return r
}
func (s *ResultStore) commit(d resultDisk) error {
	if err := s.save(d); err != nil {
		return fmt.Errorf("persist result store: %w", err)
	}
	s.data = d
	return nil
}
func (s *ResultStore) Reserve(in ResultInput) error {
	in.ExpiresAt = in.ExpiresAt.Round(0).UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	if !resultToken(in.Owner) || !resultToken(in.ServerInstanceID) || !resultToken(in.AgentID) || !resultToken(in.IdempotencyKey) || !resultToken(in.RunID) || in.ResultID != "" || in.DeliveryID != "" || in.ExpiresAt.IsZero() || len(in.Text) > 256<<10 || len(in.Destination) > 4096 || len(in.Channel) > 64 {
		return fmt.Errorf("invalid result input reservation")
	}
	k := resultIdentity(in.Owner, in.ServerInstanceID, in.IdempotencyKey)
	if old, ok := s.data.Inputs[k]; ok {
		old.DeliveryID = ""
		old.ResultID = ""
		if !reflect.DeepEqual(old, in) {
			return fmt.Errorf("result input reservation conflict")
		}
		return nil
	}
	for _, other := range s.data.Inputs {
		if other.Owner == in.Owner && other.ServerInstanceID == in.ServerInstanceID && other.RunID == in.RunID {
			return fmt.Errorf("result run already reserved under another input key")
		}
	}
	for _, answer := range s.data.Answers {
		other := answer.Input
		if other.Owner == in.Owner && other.ServerInstanceID == in.ServerInstanceID && (other.IdempotencyKey == in.IdempotencyKey || other.RunID == in.RunID) {
			return fmt.Errorf("result input conflicts with answer reservation")
		}
	}
	if len(s.data.Inputs) >= resultRecordLimit {
		return fmt.Errorf("result input capacity exceeded")
	}
	d := cloneResultData(s.data)
	d.Inputs[k] = in
	return s.commit(d)
}
func (s *ResultStore) BindReceipt(owner, instance, agent, key, delivery string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := resultIdentity(owner, instance, key)
	in, ok := s.data.Inputs[k]
	if !ok || in.AgentID != agent || !resultToken(delivery) || (in.DeliveryID != "" && in.DeliveryID != delivery) {
		return fmt.Errorf("result receipt mismatch or unresolved reservation")
	}
	if in.DeliveryID == delivery {
		return nil
	}
	for _, other := range s.data.Inputs {
		if other.Owner == owner && other.ServerInstanceID == instance && other.DeliveryID == delivery {
			return fmt.Errorf("result receipt delivery reused")
		}
	}
	for _, answer := range s.data.Answers {
		other := answer.Input
		if other.Owner == owner && other.ServerInstanceID == instance && other.DeliveryID == delivery {
			return fmt.Errorf("result receipt delivery reused by answer")
		}
	}
	d := cloneResultData(s.data)
	in.DeliveryID = delivery
	d.Inputs[k] = in
	return s.commit(d)
}

// validateSummaryRun binds optional legacy aliases to the original local run.
// An engine turnId is deliberately excluded from this local identity check.
func validateSummaryRun(frame Frame, run string) error {
	for _, fields := range []Frame{frame, payloadOf(frame)} {
		for _, key := range []string{"runId", "run_id"} {
			if value, ok := fields[key]; ok && value != run {
				return fmt.Errorf("summary result local run mismatch")
			}
		}
	}
	return nil
}
func (s *ResultStore) Apply(owner string, frame Frame, now time.Time) (ResultRecord, bool, error) {
	agent, p, err := ParseSummaryResult(frame)
	if err != nil {
		return ResultRecord{}, false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := resultIdentity(owner, p.ServerInstanceID, p.ResultID)
	if old, ok := s.data.Results[key]; ok {
		if len(old.Inputs) == 1 {
			if err := validateSummaryRun(frame, old.Inputs[0].RunID); err != nil {
				return ResultRecord{}, false, err
			}
		}
		if old.AgentID != agent || !reflect.DeepEqual(old.Payload, p) {
			return ResultRecord{}, false, fmt.Errorf("summary result identity changed content")
		}
		return cloneResult(old), false, nil
	}
	if len(s.data.Results) >= resultRecordLimit {
		return ResultRecord{}, false, fmt.Errorf("result capacity exceeded")
	}
	r := ResultRecord{Owner: owner, AgentID: agent, Payload: p, SpeechState: "pending"}
	if utf8.RuneCountInString(p.FullText) > 2000 {
		r.SpeechState = "suppressed"
	}
	for i, m := range p.Correlation.Inputs {
		in, ok := s.data.Inputs[resultIdentity(owner, p.ServerInstanceID, m.IdempotencyKey)]
		if !ok || in.AgentID != agent || in.DeliveryID != m.DeliveryID || in.ResultID != "" {
			return ResultRecord{}, false, fmt.Errorf("summary result unresolved or conflicting member")
		}
		if p.Correlation.Scope == "input" {
			if err := validateSummaryRun(frame, in.RunID); err != nil {
				return ResultRecord{}, false, err
			}
		}
		r.Inputs = append(r.Inputs, in)
		if i == 0 {
			r.Channel = in.Channel
			r.Destination = in.Destination
		}
		if in.Channel != "voice" || in.Channel != r.Channel || in.Destination != r.Destination || !now.Before(in.ExpiresAt) {
			r.SpeechState = "suppressed"
		}
	}
	d := cloneResultData(s.data)
	d.Results[key] = r
	delete(d.Staged, key)
	for _, in := range r.Inputs {
		k := resultIdentity(owner, p.ServerInstanceID, in.IdempotencyKey)
		in.ResultID = p.ResultID
		d.Inputs[k] = in
	}
	if err = s.commit(d); err != nil {
		return ResultRecord{}, false, err
	}
	return cloneResult(r), true, nil
}

// Claim persists playback intent before returning it. A lost acknowledgment is
// never automatically retried; reopening turns claimed records into uncertain.
func (s *ResultStore) Claim(owner, instance, id string, now time.Time) (ResultRecord, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := resultIdentity(owner, instance, id)
	r, ok := s.data.Results[k]
	if !ok || r.SpeechState != "pending" {
		return cloneResult(r), false, nil
	}
	r.SpeechState = "claimed"
	for _, in := range r.Inputs {
		if !now.Before(in.ExpiresAt) {
			r.SpeechState = "suppressed"
		}
	}
	d := cloneResultData(s.data)
	d.Results[k] = r
	if err := s.commit(d); err != nil {
		return ResultRecord{}, false, err
	}
	return cloneResult(r), r.SpeechState == "claimed", nil
}
func (s *ResultStore) Finish(owner, instance, id, state string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if state != "spoken" && state != "accepted" && state != "uncertain" && state != "suppressed" {
		return fmt.Errorf("invalid result speech outcome")
	}
	k := resultIdentity(owner, instance, id)
	r, ok := s.data.Results[k]
	if !ok {
		return fmt.Errorf("unknown result")
	}
	if r.SpeechState == state {
		return nil
	}
	if r.SpeechState != "claimed" {
		return fmt.Errorf("result speech was not claimed")
	}
	r.SpeechState = state
	d := cloneResultData(s.data)
	d.Results[k] = r
	return s.commit(d)
}
func (s *ResultStore) Get(owner, instance, id string) (ResultRecord, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.data.Results[resultIdentity(owner, instance, id)]
	return cloneResult(r), ok
}
func (s *ResultStore) Inputs() []ResultInput {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]ResultInput, 0, len(s.data.Inputs))
	for _, in := range s.data.Inputs {
		out = append(out, in)
	}
	return out
}

// Results returns detached records for reconciliation and outbox inspection.
func (s *ResultStore) Results() []ResultRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]ResultRecord, 0, len(s.data.Results))
	for _, r := range s.data.Results {
		out = append(out, cloneResult(r))
	}
	return out
}

// Stage durably retains an event before transport replay advances. Unresolved or
// mismatched membership remains inspectable and cannot complete any input.
func (s *ResultStore) Stage(owner string, frame Frame) error {
	agent, p, err := ParseSummaryResult(frame)
	if err != nil {
		return err
	}
	if !resultToken(owner) {
		return fmt.Errorf("invalid result owner")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	k := resultIdentity(owner, p.ServerInstanceID, p.ResultID)
	if p.Correlation.Scope == "input" {
		if in, ok := s.data.Inputs[resultIdentity(owner, p.ServerInstanceID, p.Correlation.Inputs[0].IdempotencyKey)]; ok {
			if err := validateSummaryRun(frame, in.RunID); err != nil {
				return err
			}
		}
	}
	if r, ok := s.data.Results[k]; ok {
		if len(r.Inputs) == 1 {
			if err := validateSummaryRun(frame, r.Inputs[0].RunID); err != nil {
				return err
			}
		}
		if r.AgentID != agent || !reflect.DeepEqual(r.Payload, p) {
			return fmt.Errorf("summary result identity changed content")
		}
		return nil
	}
	if old, ok := s.data.Staged[k]; ok {
		a, op, e := ParseSummaryResult(old.Frame)
		if e != nil || a != agent || !reflect.DeepEqual(op, p) {
			return fmt.Errorf("staged summary result identity changed content")
		}
		return nil
	}
	if len(s.data.Staged) >= resultRecordLimit {
		return fmt.Errorf("staged result capacity exceeded")
	}
	d := cloneResultData(s.data)
	if d.Staged == nil {
		d.Staged = map[string]StagedResult{}
	}
	b, _ := json.Marshal(frame)
	var copyFrame Frame
	_ = json.Unmarshal(b, &copyFrame)
	d.Staged[k] = StagedResult{Owner: owner, Frame: copyFrame}
	return s.commit(d)
}
func (s *ResultStore) Staged() []StagedResult {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := cloneResultData(s.data)
	out := make([]StagedResult, 0, len(d.Staged))
	for _, r := range d.Staged {
		out = append(out, r)
	}
	return out
}

// BindQuestion links an engine question to one original task, never to the next
// answer command. The mapping remains durable for retries after the UI closes.
func (s *ResultStore) BindQuestion(owner, instance, agent, questionID, taskKey string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !resultToken(questionID) {
		return fmt.Errorf("invalid question identity")
	}
	parent, ok := s.data.Inputs[resultIdentity(owner, instance, taskKey)]
	if !ok || parent.AgentID != agent {
		return fmt.Errorf("question parent mismatch")
	}
	q := ResultQuestion{Owner: owner, ServerInstanceID: instance, AgentID: agent, QuestionID: questionID, TaskKey: taskKey}
	key := resultIdentity(owner, instance, agent, questionID)
	if old, ok := s.data.Questions[key]; ok {
		if old != q {
			return fmt.Errorf("question linkage changed")
		}
		return nil
	}
	if parent.ResultID != "" {
		return fmt.Errorf("question parent already resolved")
	}
	if len(s.data.Questions) >= resultRecordLimit {
		return fmt.Errorf("question capacity exceeded")
	}
	d := cloneResultData(s.data)
	if d.Questions == nil {
		d.Questions = map[string]ResultQuestion{}
	}
	d.Questions[key] = q
	return s.commit(d)
}

// ReserveAnswer stores a command independently of summary membership. Only its
// parent task may be completed by the eventual correlated summary.
var ErrResultQuestionUnbound = errors.New("answer question has no recorded local task")

func (s *ResultStore) ReserveAnswer(in ResultInput, questionID string) (ResultInput, error) {
	return s.reserveAnswer(in, questionID, false)
}

// ReserveUnlinkedAnswer journals an app-origin answer without inventing a task.
func (s *ResultStore) ReserveUnlinkedAnswer(in ResultInput, questionID string) error {
	_, err := s.reserveAnswer(in, questionID, true)
	return err
}
func (s *ResultStore) reserveAnswer(in ResultInput, questionID string, unlinked bool) (ResultInput, error) {
	in.ExpiresAt = in.ExpiresAt.Round(0).UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	fail := func(message string) (ResultInput, error) { return ResultInput{}, fmt.Errorf("%s", message) }
	if !resultToken(in.Owner) || !resultToken(in.ServerInstanceID) || !resultToken(in.AgentID) || !resultToken(in.IdempotencyKey) || !resultToken(in.RunID) || !resultToken(questionID) || in.ResultID != "" || in.DeliveryID != "" || in.ExpiresAt.IsZero() || len(in.Text) > 256<<10 || len(in.Destination) > 4096 || len(in.Channel) > 64 {
		return fail("invalid answer reservation")
	}
	key := resultIdentity(in.Owner, in.ServerInstanceID, in.IdempotencyKey)
	if old, ok := s.data.Answers[key]; ok {
		original := old.Input
		original.DeliveryID = ""
		if (unlinked && old.Parent.IdempotencyKey != "") || old.QuestionID != questionID || !reflect.DeepEqual(original, in) {
			return fail("answer reservation changed")
		}
		return old.Parent, nil
	}
	if _, ok := s.data.Inputs[key]; ok {
		return fail("answer key belongs to task")
	}
	q, ok := s.data.Questions[resultIdentity(in.Owner, in.ServerInstanceID, in.AgentID, questionID)]
	var parent ResultInput
	if !ok {
		for _, known := range s.data.Questions {
			if known.QuestionID == questionID {
				return fail("answer question identity mismatch")
			}
		}
		if !unlinked {
			return ResultInput{}, ErrResultQuestionUnbound
		}
	} else {
		if unlinked {
			return fail("answer question already has a local task")
		}
		parent, ok = s.data.Inputs[resultIdentity(in.Owner, in.ServerInstanceID, q.TaskKey)]
		if !ok || parent.AgentID != in.AgentID || parent.ResultID != "" {
			return fail("answer parent missing or resolved")
		}
	}
	for _, old := range s.data.Inputs {
		if old.Owner == in.Owner && old.ServerInstanceID == in.ServerInstanceID && old.RunID == in.RunID {
			return fail("answer run belongs to task")
		}
	}
	for _, old := range s.data.Answers {
		if old.Input.Owner == in.Owner && old.Input.ServerInstanceID == in.ServerInstanceID && old.Input.RunID == in.RunID {
			return fail("answer run already reserved")
		}
	}
	if len(s.data.Answers) >= resultRecordLimit {
		return fail("answer capacity exceeded")
	}
	d := cloneResultData(s.data)
	if d.Answers == nil {
		d.Answers = map[string]ResultAnswer{}
	}
	d.Answers[key] = ResultAnswer{Input: in, QuestionID: questionID, Parent: parent}
	if err := s.commit(d); err != nil {
		return ResultInput{}, err
	}
	return parent, nil
}
func (s *ResultStore) Answers() []ResultAnswer {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]ResultAnswer, 0, len(s.data.Answers))
	for _, answer := range s.data.Answers {
		out = append(out, answer)
	}
	return out
}
func (s *ResultStore) BindAnswerReceipt(owner, instance, agent, key, delivery, state string) (ResultAnswer, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fail := func(message string) (ResultAnswer, error) { return ResultAnswer{}, fmt.Errorf("%s", message) }
	rank := map[string]int{"unknown": 0, "queued": 1, "delivered": 2, "started": 3, "completed": 4, "rejected": 4}
	if _, ok := rank[state]; !ok || !resultToken(delivery) {
		return fail("invalid answer receipt")
	}
	k := resultIdentity(owner, instance, key)
	answer, ok := s.data.Answers[k]
	if !ok || answer.Input.AgentID != agent || (answer.Input.DeliveryID != "" && answer.Input.DeliveryID != delivery) {
		return fail("answer receipt mismatch")
	}
	for _, in := range s.data.Inputs {
		if in.Owner == owner && in.ServerInstanceID == instance && in.DeliveryID == delivery {
			return fail("answer delivery belongs to task")
		}
	}
	for otherKey, other := range s.data.Answers {
		in := other.Input
		if otherKey != k && in.Owner == owner && in.ServerInstanceID == instance && in.DeliveryID == delivery {
			return fail("answer delivery reused")
		}
	}
	if answer.ReceiptState != "" && rank[answer.ReceiptState] == 4 && rank[state] == 4 && answer.ReceiptState != state {
		return fail("answer terminal receipt changed")
	}
	if answer.Input.DeliveryID == delivery && answer.ReceiptState != "" && rank[answer.ReceiptState] >= rank[state] {
		return answer, nil
	}
	answer.Input.DeliveryID = delivery
	answer.ReceiptState = state
	d := cloneResultData(s.data)
	d.Answers[k] = answer
	if err := s.commit(d); err != nil {
		return ResultAnswer{}, err
	}
	return answer, nil
}
