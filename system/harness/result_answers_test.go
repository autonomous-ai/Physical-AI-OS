package harness

import (
	"errors"
	"testing"
	"time"
)

func answerInput() ResultInput {
	return ResultInput{Owner: "owner", ServerInstanceID: "instance", AgentID: "agent", IdempotencyKey: "answer", RunID: "answer-run", Text: `{"choice":"red"}`, Channel: "voice", Destination: "lamp", ExpiresAt: time.Now().Add(time.Hour)}
}
func TestResultAnswerAcknowledgmentNeverCompletesParent(t *testing.T) {
	s, path := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	if err := s.BindQuestion("owner", "instance", "agent", "question", "a"); err != nil {
		t.Fatal(err)
	}
	in := answerInput()
	parent, err := s.ReserveAnswer(in, "question")
	if err != nil || parent.IdempotencyKey != "a" {
		t.Fatalf("parent: %+v %v", parent, err)
	}
	answer, err := s.BindAnswerReceipt("owner", "instance", "agent", "answer", "answer-delivery", "completed")
	if err != nil || answer.ReceiptState != "completed" {
		t.Fatal(err)
	}
	if inputs := s.Inputs(); len(inputs) != 1 || inputs[0].ResultID != "" {
		t.Fatal("answer completed parent or became task", inputs)
	}
	if _, _, err = s.Apply("owner", resultFrame("answer"), time.Now()); err == nil {
		t.Fatal("answer accepted as result membership")
	}
	s, err = OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.ReserveAnswer(in, "question"); err != nil {
		t.Fatal("retry lost linkage", err)
	}
	if len(s.Answers()) != 1 || s.Answers()[0].Parent.IdempotencyKey != "a" || s.Answers()[0].ReceiptState != "completed" {
		t.Fatal("answer lost on reopen")
	}
	if _, _, err = s.Apply("owner", resultFrame("a"), time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err = s.ReserveAnswer(in, "question"); err != nil {
		t.Fatal("exact retry after parent completed failed", err)
	}
	in.IdempotencyKey = "new-answer"
	in.RunID = "new-run"
	if _, err = s.ReserveAnswer(in, "question"); err == nil {
		t.Fatal("new answer to completed parent accepted")
	}
}
func TestResultQuestionAndAnswerCannotRebindIdentity(t *testing.T) {
	s, _ := testResultStore(t)
	reserveResult(t, s, "a", "lamp", true)
	reserveResult(t, s, "b", "lamp", true)
	if err := s.BindQuestion("owner", "instance", "agent", "question", "a"); err != nil {
		t.Fatal(err)
	}
	if err := s.BindQuestion("owner", "instance", "agent", "question", "b"); err == nil {
		t.Fatal("question retargeted")
	}
	for _, field := range []string{"owner", "agent", "instance"} {
		in := answerInput()
		switch field {
		case "owner":
			in.Owner = "other"
		case "agent":
			in.AgentID = "other"
		case "instance":
			in.ServerInstanceID = "other"
		}
		if _, err := s.ReserveAnswer(in, "question"); err == nil || errors.Is(err, ErrResultQuestionUnbound) {
			t.Fatal("explicit identity conflict treated unbound", field, err)
		}
	}
	in := answerInput()
	if _, err := s.ReserveAnswer(in, "question"); err != nil {
		t.Fatal(err)
	}
	changed := in
	changed.Text = "changed"
	if _, err := s.ReserveAnswer(changed, "question"); err == nil {
		t.Fatal("changed answer accepted")
	}
	if _, err := s.BindAnswerReceipt("owner", "instance", "agent", "answer", "delivery-a", "completed"); err == nil {
		t.Fatal("answer used parent delivery")
	}
	if _, err := s.BindAnswerReceipt("owner", "instance", "agent", "a", "answer-delivery", "completed"); err == nil {
		t.Fatal("answer receipt bound to task key")
	}
	if _, err := s.BindAnswerReceipt("owner", "instance", "agent", "answer", "answer-delivery", "rejected"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.BindAnswerReceipt("owner", "instance", "agent", "answer", "changed-delivery", "rejected"); err == nil {
		t.Fatal("delivery rebound")
	}
	if _, err := s.BindAnswerReceipt("owner", "instance", "agent", "answer", "answer-delivery", "completed"); err == nil {
		t.Fatal("terminal receipt changed")
	}
	if err := s.Reserve(in); err == nil {
		t.Fatal("answer key reused as task")
	}
}
func TestResultUnlinkedAnswerRetainsNoInventedParent(t *testing.T) {
	s, path := testResultStore(t)
	in := answerInput()
	if _, err := s.ReserveAnswer(in, "app-question"); !errors.Is(err, ErrResultQuestionUnbound) {
		t.Fatal("missing unbound sentinel", err)
	}
	if err := s.ReserveUnlinkedAnswer(in, "app-question"); err != nil {
		t.Fatal(err)
	}
	if len(s.Inputs()) != 0 || len(s.Answers()) != 1 || s.Answers()[0].Parent.IdempotencyKey != "" {
		t.Fatal("invented parent task")
	}
	reserveResult(t, s, "a", "lamp", true)
	if err := s.BindQuestion("owner", "instance", "agent", "app-question", "a"); err != nil {
		t.Fatal(err)
	}
	parent, err := s.ReserveAnswer(in, "app-question")
	if err != nil || parent.IdempotencyKey != "" {
		t.Fatal("retry silently retargeted", parent, err)
	}
	s, err = OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.BindAnswerReceipt("owner", "instance", "agent", "answer", "answer-delivery", "completed"); err != nil {
		t.Fatal(err)
	}
	if s.Answers()[0].Parent.IdempotencyKey != "" || s.Inputs()[0].ResultID != "" {
		t.Fatal("unlinked receipt completed task")
	}
	in.IdempotencyKey = "new"
	in.RunID = "new"
	if err = s.ReserveUnlinkedAnswer(in, "app-question"); err == nil {
		t.Fatal("known parent bypassed")
	}
}
