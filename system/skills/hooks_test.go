package skills

import "testing"

// Devices without `expression` drop emotion-acknowledge but keep turn-gate.
func TestSupportedHooks_GatesExpressionHook(t *testing.T) {
	withExpression := SupportedHooks(map[string]bool{"expression": true})
	if !contains(withExpression, "emotion-acknowledge") || !contains(withExpression, "turn-gate") {
		t.Fatalf("expression device should keep all hooks, got %v", withExpression)
	}

	noExpression := SupportedHooks(map[string]bool{"audio": true, "sensing": true, "presence": true})
	if contains(noExpression, "emotion-acknowledge") {
		t.Errorf("emotion-acknowledge must be pruned without expression, got %v", noExpression)
	}
	if !contains(noExpression, "turn-gate") {
		t.Errorf("turn-gate (no capability) must always survive, got %v", noExpression)
	}
}

// Empty capabilities fail open to every hook.
func TestSupportedHooks_FailOpen(t *testing.T) {
	if got := SupportedHooks(nil); len(got) != len(Hooks) {
		t.Fatalf("nil caps: got %d hooks, want all %d (fail-open)", len(got), len(Hooks))
	}
}

// Every capability the hook map references must be a real ROBOT.md capability.
func TestHookCapability_KnownCapabilities(t *testing.T) {
	known := map[string]bool{
		"audio": true, "vision": true, "sensing": true, "presence": true,
		"motion": true, "light": true, "display": true, "expression": true, "media": true,
		"connectivity": true, "system": true,
	}
	for hook, cap := range HookCapability {
		if !known[cap] {
			t.Errorf("hook %q maps to unknown capability %q", hook, cap)
		}
		if !contains(Hooks, hook) {
			t.Errorf("hook %q in HookCapability is not in Hooks", hook)
		}
	}
}
