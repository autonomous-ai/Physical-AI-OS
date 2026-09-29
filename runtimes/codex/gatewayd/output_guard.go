package gatewayd

import (
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

const (
	degenerateOutputMinBytes = 64 * 1024
	degenerateOutputMinWords = 4096
	degenerateOutputError    = "degenerate_output: repetitive model output rejected; session quarantined; inspect task state before continuing"
)

// degenerateAssistantOutput recognizes the observed runaway syllable loop, not
// merely a long answer.
// At least 95% of a large message must consist of at most eight short alphabetic tokens, each
// repeated at least 128 times.
func degenerateAssistantOutput(text string) bool {
	if len(text) < degenerateOutputMinBytes {
		return false
	}
	counts := make(map[string]int)
	total := 0
	for field := range strings.FieldsSeq(text) {
		total++
		if utf8.RuneCountInString(field) > 8 {
			continue
		}
		letters := true
		for _, r := range field {
			if !unicode.IsLetter(r) {
				letters = false
				break
			}
		}
		if !letters {
			continue
		}
		key := strings.ToLower(field)
		if _, exists := counts[key]; !exists && len(counts) >= 1024 {
			return false
		}
		counts[key]++
	}
	if total < degenerateOutputMinWords {
		return false
	}
	frequencies := make([]int, 0, len(counts))
	for _, count := range counts {
		if count >= 128 {
			frequencies = append(frequencies, count)
		}
	}
	sort.Sort(sort.Reverse(sort.IntSlice(frequencies)))
	dominated := 0
	for i, count := range frequencies {
		if i == 8 {
			break
		}
		dominated += count
	}
	return dominated*100 >= total*95
}
