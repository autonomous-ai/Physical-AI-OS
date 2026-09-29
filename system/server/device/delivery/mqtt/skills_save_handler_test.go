package mqtthandler

import (
	"encoding/json"
	"fmt"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

func TestParseSkillsSaveData(t *testing.T) {
	// Whitespace must be trimmed before it reaches the gateway — a name with a
	// trailing space would otherwise fail ValidateSkillName for no good reason.
	draft, errMsg := parseSkillsSaveData(json.RawMessage(
		`{"name":"  weekly-report  ","description":" Sums up the week. ","instructions":"\n1. Collect\n"}`))
	if errMsg != "" {
		t.Fatalf("unexpected error: %s", errMsg)
	}
	want := domain.SkillDraft{
		Name:         "weekly-report",
		Description:  "Sums up the week.",
		Instructions: "1. Collect",
	}
	if draft != want {
		t.Fatalf("draft = %+v, want %+v", draft, want)
	}
}

func TestParseSkillsSaveDataRejectsIncomplete(t *testing.T) {
	cases := []struct{ label, payload string }{
		{"malformed json", `{"name":`},
		{"empty object", `{}`},
		{"missing description", `{"name":"x","instructions":"i"}`},
		{"missing instructions", `{"name":"x","description":"d"}`},
		{"missing name", `{"description":"d","instructions":"i"}`},
		{"whitespace-only name", `{"name":"   ","description":"d","instructions":"i"}`},
		{"whitespace-only instructions", `{"name":"x","description":"d","instructions":" \n\t "}`},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			draft, errMsg := parseSkillsSaveData(json.RawMessage(tc.payload))
			if errMsg == "" {
				t.Fatalf("expected a failure message, got draft %+v", draft)
			}
			if draft != (domain.SkillDraft{}) {
				t.Errorf("a rejected payload must not yield a draft: %+v", draft)
			}
		})
	}
}

// Name shape is left to SaveSkill (skills.ValidateSkillName).
func TestParseSkillsSaveDataDefersNameShapeToGateway(t *testing.T) {
	draft, errMsg := parseSkillsSaveData(json.RawMessage(
		`{"name":"NOT a slug","description":"d","instructions":"i"}`))
	if errMsg != "" {
		t.Fatalf("parsing must not police the name shape, got: %s", errMsg)
	}
	if draft.Name != "NOT a slug" {
		t.Errorf("name = %q, want it passed through verbatim", draft.Name)
	}
	if err := skills.ValidateSkillName(draft.Name); err == nil {
		t.Error("skills.ValidateSkillName should reject this name")
	}
}

func TestClassifySkillsSaveError(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{domain.ErrNotSupportedByRuntime, "unsupported_runtime"},
		{skills.ErrInvalidSkillName, "validate_name"},
		{skills.ErrSkillExists, "already_exists"},
		{fmt.Errorf("disk full"), "write"},
		// Wrapped sentinels must still classify — SaveSkill wraps with %w.
		{fmt.Errorf("save: %w", skills.ErrSkillExists), "already_exists"},
		{fmt.Errorf("gateway: %w", domain.ErrNotSupportedByRuntime), "unsupported_runtime"},
	}
	for _, tc := range cases {
		if got := classifySkillsSaveError(tc.err); got != tc.want {
			t.Errorf("classify(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

// skills.save is its own kind, distinct from skills.install.
func TestSkillsSaveKindIsDistinct(t *testing.T) {
	if domain.KindSkillsSave == domain.KindSkillsInstall {
		t.Fatal("skills.save and skills.install must be distinct kinds")
	}
	if domain.KindSkillsSave != "skills.save" {
		t.Errorf("kind = %q, want skills.save", domain.KindSkillsSave)
	}
}

// skills.install_store is its own kind, distinct from skills.install.
func TestSkillsInstallStoreKindIsDistinct(t *testing.T) {
	if domain.KindSkillsInstallStore == domain.KindSkillsInstall {
		t.Fatal("skills.install_store must not reuse the role-bundle kind")
	}
	if domain.KindSkillsInstallStore == domain.KindSkillsSave {
		t.Fatal("skills.install_store and skills.save must be distinct kinds")
	}
	if domain.KindSkillsInstallStore != "skills.install_store" {
		t.Errorf("kind = %q, want skills.install_store", domain.KindSkillsInstallStore)
	}
}

// The role-bundle payload must stay role-only — no id field crept into it.
func TestSkillsInstallDataStaysRoleOnly(t *testing.T) {
	var req domain.MQTTSkillsInstallData
	if err := json.Unmarshal([]byte(`{"role":"design","id":"abc"}`), &req); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if req.Role != "design" {
		t.Errorf("role = %q", req.Role)
	}
	// An `id` in a skills.install payload is simply ignored — that kind never
	// installs a catalog skill.
	if fmt.Sprintf("%+v", req) != "{Role:design}" {
		t.Errorf("MQTTSkillsInstallData gained a field: %+v", req)
	}
}

func TestSkillsInstallStoreData(t *testing.T) {
	var req domain.MQTTSkillsInstallStoreData
	if err := json.Unmarshal([]byte(`{"id":"6a195e59e438b1a9f06299d0","name":"design-critique"}`), &req); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if req.ID != "6a195e59e438b1a9f06299d0" || req.Name != "design-critique" {
		t.Errorf("got %+v", req)
	}
	// name is optional — only a fallback when the archive has no wrapping dir.
	var bare domain.MQTTSkillsInstallStoreData
	if err := json.Unmarshal([]byte(`{"id":"abc"}`), &bare); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if bare.Name != "" {
		t.Errorf("name should default empty, got %q", bare.Name)
	}
}

func TestClassifySkillsInstallStoreError(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{domain.ErrNotSupportedByRuntime, "unsupported_runtime"},
		{skills.ErrEmptyArchive, "archive"},
		{skills.ErrInvalidSkillName, "validate_name"},
		{fmt.Errorf("disk full"), "install"},
		// InstallSkillArchive wraps with %w — sentinels must survive.
		{fmt.Errorf("extract: %w", skills.ErrEmptyArchive), "archive"},
	}
	for _, tc := range cases {
		if got := classifySkillsInstallStoreError(tc.err); got != tc.want {
			t.Errorf("classify(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

// Catalog ids with separators are rejected before the request is built.
func TestValidateStoreSkillIDGuardsPath(t *testing.T) {
	if err := skills.ValidateStoreSkillID("6a195e59e438b1a9f06299d0"); err != nil {
		t.Errorf("a normal catalog id must pass: %v", err)
	}
	for _, bad := range []string{"", "   ", "../secret", "a/b", "a?x=1", "a#frag", `a\b`} {
		if err := skills.ValidateStoreSkillID(bad); err == nil {
			t.Errorf("id %q must be rejected", bad)
		}
	}
}

func TestClassifySkillsUninstallError(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{domain.ErrNotSupportedByRuntime, "unsupported_runtime"},
		{skills.ErrSkillNotFound, "not_found"},
		{skills.ErrInvalidSkillName, "validate_name"},
		{fmt.Errorf("device busy"), "remove"},
		// DeleteSkill wraps with %w — sentinels must survive.
		{fmt.Errorf("remove: %w", skills.ErrSkillNotFound), "not_found"},
	}
	for _, tc := range cases {
		if got := classifySkillsUninstallError(tc.err); got != tc.want {
			t.Errorf("classify(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

func TestSkillsUninstallKindIsDistinct(t *testing.T) {
	if domain.KindSkillsUninstall != "skills.uninstall" {
		t.Errorf("kind = %q, want skills.uninstall", domain.KindSkillsUninstall)
	}
	for _, other := range []string{domain.KindSkillsInstall, domain.KindSkillsInstallStore, domain.KindSkillsSave, domain.KindSkillsFiles} {
		if domain.KindSkillsUninstall == other {
			t.Errorf("skills.uninstall collides with %q", other)
		}
	}
}
