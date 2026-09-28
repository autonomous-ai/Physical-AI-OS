package device

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// Runtime implementations import device, so importing them here to call
// SupportedChannels would create an import cycle. Read the domain's channel
// constants instead: newly declared channels must obey PATCH semantics even
// before every runtime supports them. Parse declarations, not source spelling
// or a duplicate allowlist that could silently miss a new channel.
func declaredContractChannels(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob("../domain/*.go")
	if err != nil {
		t.Fatal(err)
	}
	channels := make(map[string]bool)
	for _, path := range paths {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, declaration := range file.Decls {
			decl, ok := declaration.(*ast.GenDecl)
			if !ok || decl.Tok != token.CONST {
				continue
			}
			for _, specification := range decl.Specs {
				spec := specification.(*ast.ValueSpec)
				for i, name := range spec.Names {
					if !strings.HasPrefix(name.Name, "Channel") {
						continue
					}
					if i >= len(spec.Values) {
						t.Fatalf("channel constant %s needs evaluation support in this test", name.Name)
					}
					literal, ok := spec.Values[i].(*ast.BasicLit)
					if !ok || literal.Kind != token.STRING {
						t.Fatalf("channel constant %s is not a string literal; extend test discovery", name.Name)
					}
					channel, err := strconv.Unquote(literal.Value)
					if err != nil || channel == "" {
						t.Fatalf("invalid channel constant %s: %s", name.Name, literal.Value)
					}
					channels[channel] = true
				}
			}
		}
	}
	if len(channels) == 0 {
		t.Fatal("no channel constants discovered")
	}
	result := make([]string, 0, len(channels))
	for channel := range channels {
		result = append(result, channel)
	}
	sort.Strings(result)
	return result
}

// Probe channelFields rather than maintaining a second list of credentials.
// Any new string field included in the snapshot is seeded automatically, even
// when its config name does not match the channel name (e.g. a bridge provider).
func populatedChannelContractConfig(t *testing.T, channel string) *config.Config {
	t.Helper()
	c := baseConfig()
	fields := reflect.ValueOf(c).Elem()
	for i := 0; i < fields.NumField(); i++ {
		field := fields.Field(i)
		if !field.CanSet() || field.Kind() != reflect.String {
			continue
		}
		before := channelFields(c)
		original := field.String()
		field.SetString("saved-" + fields.Type().Field(i).Name)
		if channelFields(c) == before {
			field.SetString(original)
		}
	}
	c.Channel = channel
	snapshot := reflect.ValueOf(channelFields(c))
	for i := 0; i < snapshot.NumField(); i++ {
		if snapshot.Field(i).IsZero() {
			t.Fatalf("channel snapshot field %s was not seeded; extend fixture for its type", snapshot.Type().Field(i).Name)
		}
	}
	return c
}

func TestChannelContractPartialConfigPreservesCredentials(t *testing.T) {
	requests := []struct {
		name string
		data domain.UpdateConfigRequest
	}{
		{name: "empty"},
		{name: "llm_only", data: domain.UpdateConfigRequest{LLMModel: "model-b"}},
	}
	for _, channel := range declaredContractChannels(t) {
		for _, request := range requests {
			t.Run(channel+"/"+request.name, func(t *testing.T) {
				c := populatedChannelContractConfig(t, channel)
				before := channelFields(c)
				changes := applyUpdate(c, request.data, "")
				if after := channelFields(c); after != before {
					t.Errorf("partial config update erased or changed channel fields:\nbefore: %+v\nafter:  %+v", before, after)
				}
				if changes.channel {
					t.Error("partial config update requested an unrelated channel reconfiguration")
				}
				if request.data.LLMModel != "" && c.LLMModel != request.data.LLMModel {
					t.Error("LLM-only update did not apply the requested model")
				}
			})
		}
	}
}
