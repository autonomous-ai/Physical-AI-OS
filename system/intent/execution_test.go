package intent

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"testing"

	"go.autonomous.ai/os/system/device"
)

type intentTestTransport func(*http.Request) (*http.Response, error)

func (f intentTestTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func routeIntentHAL(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	target, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	original := http.DefaultTransport
	http.DefaultTransport = intentTestTransport(func(request *http.Request) (*http.Response, error) {
		cloned := request.Clone(request.Context())
		cloned.URL.Scheme = target.Scheme
		cloned.URL.Host = target.Host
		return original.RoundTrip(cloned)
	})
	t.Cleanup(func() { http.DefaultTransport = original })
	previous := deviceCaps
	Configure(nil)
	t.Cleanup(func() { Configure(previous) })
}

func TestExecutionFailurePreservesRemainingActions(t *testing.T) {
	for _, failedPath := range []string{"", "/led/effect/stop", "/led/solid"} {
		t.Run("failed="+failedPath, func(t *testing.T) {
			var paths []string
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/emotion/status" {
					fmt.Fprint(w, `{"sleeping":false}`)
					return
				}
				paths = append(paths, r.URL.Path)
				if r.URL.Path == failedPath {
					w.WriteHeader(http.StatusInternalServerError)
				}
			})
			result := MatchCommands("set the light red")
			if result == nil || result.ExecutionFailed != (failedPath != "") {
				t.Fatalf("execution result = %+v, failed path %q", result, failedPath)
			}
			if (failedPath == "" && (result.TTSText != "Red light on!" || !result.LEDChanged)) || (failedPath != "" && (result.TTSText == "Red light on!" || result.LEDChanged)) || len(result.Actions) != 2 {
				t.Fatalf("response/actions changed: %+v", result)
			}
			if !reflect.DeepEqual(paths, []string{"/led/effect/stop", "/led/solid"}) {
				t.Fatalf("HAL calls = %v", paths)
			}
		})
	}
}

func TestEmotionExecutionFailureAndCapabilitySkip(t *testing.T) {
	var paths []string
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/emotion/status" {
			fmt.Fprint(w, `{"sleeping":false}`)
			return
		}
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/emotion" {
			w.WriteHeader(http.StatusBadGateway)
		}
	})
	result := MatchCommands("light on")
	if result == nil || !result.ExecutionFailed {
		t.Fatalf("emotion error was not propagated: %+v", result)
	}
	Configure(map[string]bool{device.CapLight: true})
	paths = nil
	result = MatchCommands("light on")
	if result == nil || result.ExecutionFailed {
		t.Fatalf("disabled expression should not fail: %+v", result)
	}
	if !reflect.DeepEqual(paths, []string{"/led/solid"}) {
		t.Fatalf("HAL calls = %v", paths)
	}
}
