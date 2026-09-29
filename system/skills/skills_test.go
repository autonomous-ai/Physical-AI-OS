package skills

import "testing"

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

// enabled is the catalog minus skills switched off in skill.json.
func enabled() int { return len(Catalog) - len(Disabled) }

// A maximal device (every capability) keeps the full catalog.
func TestSupported_MaximalDeviceKeepsAll(t *testing.T) {
	caps := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "companion": true, "system": true, "environment": true,
	}
	got := Supported(caps)
	if len(got) != enabled() {
		t.Fatalf("maximal device: got %d skills, want full catalog %d", len(got), enabled())
	}
	for name := range Disabled {
		if contains(got, name) {
			t.Errorf("disabled skill %q must never be supported", name)
		}
	}
}

// Empty capabilities preserve legacy skills without opting into environment.
func TestSupported_FailOpenPreservesLegacyOnly(t *testing.T) {
	for _, caps := range []map[string]bool{nil, {}} {
		got := Supported(caps)
		for _, name := range Catalog {
			if name == "environment" || Disabled[name] {
				if contains(got, name) {
					t.Errorf("missing capabilities must not install %q", name)
				}
			} else if !contains(got, name) {
				t.Errorf("legacy fail-open must preserve %q", name)
			}
		}
		if len(got) != enabled()-1 {
			t.Errorf("got %d skills, want %d legacy skills", len(got), enabled()-1)
		}
	}
}

// A reduced device drops unsupported hardware skills; platform skills survive.
func TestSupported_ReducedDevicePrunesHardware(t *testing.T) {
	// Speaker-only box (like intern-v2): audio + sensing.
	got := Supported(map[string]bool{"audio": true, "sensing": true})

	// Camera people-perception needs presence (pruned); voice people-perception needs audio (kept).
	for _, gone := range []string{"servo-control", "servo-tracking", "led-control", "display", "emotion", "scene", "camera", "music", "face-enroll", "guard", "computer-use", "environment"} {
		if contains(got, gone) {
			t.Errorf("expected %q pruned (device lacks its capability)", gone)
		}
	}
	for _, kept := range []string{"audio", "voice", "sensing", "sensing-track", "speaker-recognizer", "user-emotion-detection"} {
		if !contains(got, kept) {
			t.Errorf("expected %q kept (audio/sensing satisfied)", kept)
		}
	}
	for _, kept := range []string{"wellbeing", "mood", "habit", "connectors", "music-suggestion", "input-branching"} {
		if !contains(got, kept) {
			t.Errorf("expected platform skill %q kept", kept)
		}
	}
}

// Map capabilities must be real ROBOT.md keys and mapped skills must exist.
func TestCapability_Consistency(t *testing.T) {
	known := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "companion": true, "system": true, "environment": true,
	}
	for skill, caps := range Capability {
		if len(caps) == 0 {
			t.Errorf("skill %q maps to an empty capability list (drop it from the map to mark it a platform skill)", skill)
		}
		for _, cap := range caps {
			if !known[cap] {
				t.Errorf("skill %q maps to unknown capability %q", skill, cap)
			}
		}
		if !contains(Catalog, skill) {
			t.Errorf("skill %q in Capability map is not in Catalog", skill)
		}
	}
}

// user-emotion-detection survives with either audio or presence.
func TestSupported_UserEmotionDetectionAnyOfSensor(t *testing.T) {
	cases := []struct {
		name string
		caps map[string]bool
		want bool
	}{
		{"mic-only (intern-v2): voice branch", map[string]bool{"audio": true, "sensing": true}, true},
		{"camera-only: face branch", map[string]bool{"vision": true, "presence": true}, true},
		{"both (lamp)", map[string]bool{"audio": true, "presence": true}, true},
		{"neither sensor", map[string]bool{"light": true, "system": true}, false},
	}
	for _, tc := range cases {
		got := contains(Supported(tc.caps), "user-emotion-detection")
		if got != tc.want {
			t.Errorf("%s: user-emotion-detection present=%v, want %v", tc.name, got, tc.want)
		}
		wantSpeaker := tc.caps["audio"]
		if gotSpeaker := contains(Supported(tc.caps), "speaker-recognizer"); gotSpeaker != wantSpeaker {
			t.Errorf("%s: speaker-recognizer present=%v, want %v (audio=%v)", tc.name, gotSpeaker, wantSpeaker, tc.caps["audio"])
		}
	}
}

// Environmental sensing does not imply a camera or an expression actuator.
func TestSupported_EnvironmentOnly(t *testing.T) {
	got := Supported(map[string]bool{"environment": true})
	for _, kept := range []string{"environment", "wellbeing"} {
		if !contains(got, kept) {
			t.Errorf("expected %q on an environment-only device", kept)
		}
	}
	for _, gone := range []string{"camera", "emotion", "sensing", "guard"} {
		if contains(got, gone) {
			t.Errorf("environment capability must not install %q", gone)
		}
	}
	if contains(Supported(map[string]bool{"environment": false}), "environment") {
		t.Error("explicit false environment capability must not install environment skill")
	}
}
