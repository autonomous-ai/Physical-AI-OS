package server

import (
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"

	"go.autonomous.ai/os/system/ota"
)

// The Versions card relies on these exact statuses; the refactor onto package
// ota must not change them.
func TestOTAHTTPStatus(t *testing.T) {
	cases := []struct {
		err  error
		want int
	}{
		{fmt.Errorf("%w: x", ota.ErrUnknownTarget), http.StatusBadRequest},
		{&ota.RateLimitedError{Target: "hal", RetryAfter: 10 * time.Second}, http.StatusTooManyRequests},
		{fmt.Errorf("%w: x", ota.ErrBuildRequest), http.StatusInternalServerError},
		{fmt.Errorf("%w: x", ota.ErrBootstrapUnreachable), http.StatusBadGateway},
		{&ota.BootstrapRefusedError{Target: "web", Status: "400 Bad Request"}, http.StatusBadGateway},
		{errors.New("decode versions: EOF"), http.StatusBadGateway},
	}
	for _, c := range cases {
		if got := otaHTTPStatus(c.err); got != c.want {
			t.Errorf("otaHTTPStatus(%v) = %d, want %d", c.err, got, c.want)
		}
	}
}
