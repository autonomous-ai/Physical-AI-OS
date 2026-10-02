package hal

import "net/http"

// RestingLEDLook is one resting LED look as HAL reports it.
type RestingLEDLook struct {
	Effect string   `json:"effect"`
	Color  []int    `json:"color"`
	Speed  *float64 `json:"speed,omitempty"`
}

// RestingLED is HAL GET/PUT /led/resting: the owner's choice, the device
// default from presets.json and the look the strip settles on.
type RestingLED struct {
	Mode      string         `json:"mode"`
	Color     []int          `json:"color"`
	Default   RestingLEDLook `json:"default"`
	Effective RestingLEDLook `json:"effective"`
}

// RestingLEDChoice is the body of HAL PUT /led/resting.
type RestingLEDChoice struct {
	Mode  string `json:"mode"`
	Color []int  `json:"color,omitempty"`
}

// RestingLEDPreview is HAL POST /led/resting/preview's reply; Painted is false
// while sleep, speech or music owns the strip.
type RestingLEDPreview struct {
	Status  string `json:"status"`
	Painted bool   `json:"painted"`
}

// GetRestingLED reads the owner's resting LED choice.
func GetRestingLED() (*RestingLED, error) {
	var out RestingLED
	if err := doJSON(http.MethodGet, "/led/resting", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SetRestingLED saves the owner's resting LED choice and shows it when resting.
func SetRestingLED(choice RestingLEDChoice) (*RestingLED, error) {
	var out RestingLED
	if err := doJSON(http.MethodPut, "/led/resting", choice, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// PreviewRestingLED paints a candidate resting colour without saving it.
func PreviewRestingLED(color []int) (*RestingLEDPreview, error) {
	var out RestingLEDPreview
	if err := doJSON(http.MethodPost, "/led/resting/preview", map[string][]int{"color": color}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
