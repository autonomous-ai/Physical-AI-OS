package http

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func TestPairConfirmRateLimitIncludesMalformedRequests(t *testing.T) {
	h := &BuddyHandler{}
	router := gin.New()
	router.POST("/confirm", h.PairConfirm)
	for i := 0; i < 11; i++ {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/confirm", strings.NewReader("{}"))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-For", "10.0.0."+strings.Repeat("1", i+1))
		router.ServeHTTP(rec, req)
		expected := http.StatusBadRequest
		if i == 10 {
			expected = http.StatusTooManyRequests
		}
		if rec.Code != expected {
			t.Fatalf("request %d: status %d, want %d", i, rec.Code, expected)
		}
		if i == 10 && rec.Header().Get("Retry-After") != "60" {
			t.Fatal("missing retry hint")
		}
	}
}

func TestPairConfirmGateConcurrentAndRecovery(t *testing.T) {
	var g pairingConfirmGate
	now := time.Now()
	var accepted atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if g.allow(now) {
				accepted.Add(1)
			}
		}()
	}
	wg.Wait()
	if accepted.Load() != 10 {
		t.Fatalf("accepted %d requests", accepted.Load())
	}
	if g.allow(now.Add(59 * time.Second)) {
		t.Fatal("window reset too soon")
	}
	if !g.allow(now.Add(time.Minute)) {
		t.Fatal("budget did not recover")
	}
}
