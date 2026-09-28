package jev

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDecodeNativeObservation(t *testing.T) {
	raw, err := json.Marshal(fixtureTree())
	if err != nil {
		t.Fatal(err)
	}
	var tree Tree
	if err = json.Unmarshal(raw, &tree); err != nil {
		t.Fatal(err)
	}
	candidates, suggestions, ok := treeCandidates(tree)
	if !ok || len(candidates) != 1 || suggestions[candidates[0].ID].Ref != "e1" || tree.Nodes[0].Secure || tree.Truncated {
		t.Fatalf("lost valid observation: %+v", tree)
	}
}

func TestDecodeRequiresExplicitSafetyBooleans(t *testing.T) {
	raw, _ := json.Marshal(fixtureTree())
	for _, field := range []string{"secure", "truncated"} {
		for _, replacement := range []string{"missing", "null", "string"} {
			t.Run(field+"/"+replacement, func(t *testing.T) {
				original := `"` + field + `":false`
				substitute := ""
				switch replacement {
				case "missing":
					original += ","
				case "null":
					substitute = `"` + field + `":null`
				case "string":
					substitute = `"` + field + `":"false"`
				}
				body := strings.Replace(string(raw), original, substitute, 1)
				if body == string(raw) {
					t.Fatal("fixture was not modified")
				}
				var tree Tree
				if err := json.Unmarshal([]byte(body), &tree); err == nil {
					t.Fatal("accepted unknown safety marker")
				}
			})
		}
	}
}

func TestDecodePreservesTrueSafetyBooleans(t *testing.T) {
	tree := fixtureTree()
	tree.Truncated = true
	tree.Nodes[0].Secure = true
	raw, _ := json.Marshal(tree)
	var decoded Tree
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	if !decoded.Truncated || !decoded.Nodes[0].Secure {
		t.Fatal("lost true safety markers")
	}
}
