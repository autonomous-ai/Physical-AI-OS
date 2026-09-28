package buddy

import (
	"log"

	"claude-desktop-buddy/httpapi"
)

// ApprovalService implements httpapi.ApprovalService: relays a Desktop approval decision over BLE and updates counters.
type ApprovalService struct {
	sm  *StateMachine
	ble *BLEServer
}

func NewApprovalService(sm *StateMachine, ble *BLEServer) *ApprovalService {
	return &ApprovalService{sm: sm, ble: ble}
}

func (a *ApprovalService) Approve(id string) error { return a.decide(id, "once") }
func (a *ApprovalService) Deny(id string) error    { return a.decide(id, "deny") }

func (a *ApprovalService) decide(id, decision string) error {
	pending := a.sm.PendingPrompt()
	if pending == nil {
		return httpapi.ErrNoPending
	}
	if pending.ID != id {
		return httpapi.ErrPromptMismatch
	}
	if err := a.ble.Send(MakePermission(id, decision)); err != nil {
		return err
	}
	if decision == "once" {
		a.sm.Approved()
		log.Printf("[approval] approved prompt %s", id)
	} else {
		a.sm.Denied()
		log.Printf("[approval] denied prompt %s", id)
	}
	return nil
}
