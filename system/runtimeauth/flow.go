// Package runtimeauth runs native account login in isolated homes. Live runtime
// files are touched only after the CLI has verified a newly acquired credential.
package runtimeauth

import (
	"context"
	"fmt"
	"os/exec"
)

type Provider struct {
	ID    string `json:"id"`
	Label string `json:"label"`
}

type Flow struct {
	Command       *exec.Cmd
	Hosts         []string
	InputRequired bool
	Verify        func(context.Context) error
	Install       func() (func() error, error)
	Close         func()
}

func Providers(runtime string) []Provider {
	switch runtime {
	case "claudecode":
		return []Provider{{"anthropic", "Claude account"}}
	case "codex":
		return []Provider{{"openai", "ChatGPT account"}}
	case "hermes", "openclaw":
		return []Provider{{"anthropic", "Claude account"}, {"openai", "ChatGPT account"}}
	default:
		return []Provider{}
	}
}

func Prepare(root, runtime, provider string) (*Flow, error) {
	supported := false
	for _, p := range Providers(runtime) {
		if p.ID == provider {
			supported = true
		}
	}
	if !supported {
		return nil, fmt.Errorf("account login is not supported for this runtime/provider")
	}
	switch runtime {
	case "claudecode":
		return prepareClaude(root)
	case "codex":
		return prepareCodex(root)
	case "hermes":
		return prepareHermes(root, provider)
	case "openclaw":
		return prepareOpenClaw(root, provider)
	}
	return nil, fmt.Errorf("unsupported runtime")
}
