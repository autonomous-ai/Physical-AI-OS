package gatewayd

import (
	"fmt"
	"strings"
	"testing"
)

func TestDegenerateAssistantOutput(t *testing.T) {
	var code strings.Builder
	for i := 0; i < 6000; i++ {
		fmt.Fprintf(&code, "func compute%d(input int) int { return input + %d } // distinct generated function\n", i, i)
	}
	cases := []struct {
		name string
		text string
		want bool
	}{
		{"observed accented syllables", "Am deschis pagina pentru a verifica disponibilitatea. " + strings.Repeat("că că căă căă că căă căă căä căä ", 16000), true},
		{"short repetition", strings.Repeat("că ", 1000), false},
		{"long code", code.String(), false},
		{"large numeric fixture", strings.Repeat("[0, 1, 2, 3, 4, 5, 6, 7],\n", 10000), false},
		{"long structured repetitive code", strings.Repeat("if (value != null) { return value; }\n", 10000), false},
		{"long explanation", strings.Repeat("This function accepts the current value and checks whether each requested operation satisfies its documented preconditions before returning the computed result.\n", 3000), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := degenerateAssistantOutput(tc.text); got != tc.want {
				t.Fatalf("classification = %v, want %v (bytes=%d)", got, tc.want, len(tc.text))
			}
		})
	}
}
