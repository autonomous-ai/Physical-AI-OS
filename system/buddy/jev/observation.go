package jev

import (
	"encoding/json"
	"errors"
)

// UnmarshalJSON rejects missing privacy/completeness markers instead of letting
// their bool zero values turn an unknown observation into an eligible one.
func (t *Tree) UnmarshalJSON(data []byte) error {
	type plain Tree
	var decoded struct {
		*plain
		Truncated *bool `json:"truncated"`
	}
	value := Tree{}
	decoded.plain = (*plain)(&value)
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	if decoded.Truncated == nil {
		return errors.New("buddy jev: observation requires explicit truncated boolean")
	}
	value.Truncated = *decoded.Truncated
	*t = value
	return nil
}

func (n *Node) UnmarshalJSON(data []byte) error {
	type plain Node
	var decoded struct {
		*plain
		Secure *bool `json:"secure"`
	}
	value := Node{}
	decoded.plain = (*plain)(&value)
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	if decoded.Secure == nil {
		return errors.New("buddy jev: node requires explicit secure boolean")
	}
	value.Secure = *decoded.Secure
	*n = value
	return nil
}
