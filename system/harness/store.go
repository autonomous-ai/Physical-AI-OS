package harness

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strings"
	"unicode/utf16"
)

var storeCapabilities = []string{"store.list", "store.inspect", "agent.prepare", "operation.get"}

// PreparationUnknownError means preparation may have been durably reserved.
// Recover by polling its operation or retrying the identical parameters and key,
// never by using a turn receipt or allocating a new preparation key.
type PreparationUnknownError struct {
	RequestID string
	Cause     error
}

func (e *PreparationUnknownError) Error() string {
	return "Harness preparation could not be confirmed; poll the retained operation or retry identical parameters and preparation key"
}
func (e *PreparationUnknownError) Unwrap() error { return e.Cause }

func isStoreOperation(kind string) bool {
	for _, value := range storeCapabilities {
		if kind == value {
			return true
		}
	}
	return false
}

var storeUUID = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$`)
var storePackage = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,63}$`)
var storeKey = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)
var storeOperationID = regexp.MustCompile(`^[a-f0-9]{64}$`)
var storeWorkspaceName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$`)

// validateStoreRequest mirrors the owner's Store v1 request schema. The CLI
// remains authoritative for installation permissions and filesystem readiness.
func validateStoreRequest(f Frame) error {
	invalid := func(field string) error { return fmt.Errorf("INVALID_REQUEST: invalid Store %s", field) }
	if !storeUUID.MatchString(stringField(f, "requestId")) {
		return invalid("requestId")
	}
	allowed := map[string]bool{"type": true, "requestId": true}
	allow := func(keys ...string) {
		for _, k := range keys {
			allowed[k] = true
		}
	}
	switch stringField(f, "type") {
	case "store.list":
		allow("query", "offset", "limit")
		if value, ok := f["query"]; ok {
			s, ok := value.(string)
			if !ok || storeStringLength(s) > 200 {
				return invalid("query")
			}
		}
		for _, bound := range []struct {
			key      string
			min, max float64
		}{{"offset", 0, 25000}, {"limit", 1, 10}} {
			if v, ok := f[bound.key]; ok {
				n, ok := storeInteger(v)
				if !ok || n < bound.min || n > bound.max {
					return invalid(bound.key)
				}
			}
		}
	case "store.inspect":
		allow("packageId")
		if !storePackage.MatchString(stringField(f, "packageId")) {
			return invalid("packageId")
		}
	case "operation.get":
		allow("operationId")
		if !storeOperationID.MatchString(stringField(f, "operationId")) {
			return invalid("operationId")
		}
	case "agent.prepare":
		allow("machineId", "packageId", "idempotencyKey", "workspace")
		if n := storeStringLength(stringField(f, "machineId")); n < 1 || n > 200 {
			return invalid("machineId")
		}
		if !storePackage.MatchString(stringField(f, "packageId")) {
			return invalid("packageId")
		}
		if !storeKey.MatchString(stringField(f, "idempotencyKey")) {
			return invalid("idempotencyKey")
		}
		var workspace Frame
		switch value := f["workspace"].(type) {
		case Frame:
			workspace = value
		case map[string]any:
			workspace = Frame(value)
		default:
			return invalid("workspace")
		}
		switch stringField(workspace, "kind") {
		case "new":
			if len(workspace) > 2 {
				return invalid("workspace")
			}
			for k := range workspace {
				if k != "kind" && k != "name" {
					return invalid("workspace")
				}
			}
			if value, ok := workspace["name"]; ok {
				name, ok := value.(string)
				if !ok || !storeWorkspaceName.MatchString(name) {
					return invalid("workspace.name")
				}
			}
		case "existing":
			path := stringField(workspace, "path")
			if len(workspace) != 2 || !strings.HasPrefix(path, "/") || storeStringLength(path) > 4096 {
				return invalid("workspace.path")
			}
			for _, r := range path {
				if r < 32 || r == 127 {
					return invalid("workspace.path")
				}
			}
		default:
			return invalid("workspace.kind")
		}
	default:
		return invalid("type")
	}
	for key := range f {
		if !allowed[key] {
			return invalid(key)
		}
	}
	return nil
}

// Runtime validators use JavaScript string lengths (UTF-16 code units).
func storeStringLength(s string) int { return len(utf16.Encode([]rune(s))) }
func storeInteger(value any) (float64, bool) {
	var n float64
	switch v := value.(type) {
	case int:
		n = float64(v)
	case int64:
		n = float64(v)
	case float64:
		n = v
	case json.Number:
		var err error
		n, err = v.Float64()
		if err != nil {
			return 0, false
		}
	default:
		return 0, false
	}
	return n, !math.IsNaN(n) && !math.IsInf(n, 0) && math.Trunc(n) == n
}
