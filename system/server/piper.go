package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// A POST is retried while HAL is not listening (a voice save restarts HAL).
// Only a failed dial is retried: a timeout may mean HAL already did the work.
const (
	piperRetryWindow   = 25 * time.Second
	piperRetryInterval = time.Second
)

// dialFailed reports whether the request never reached HAL — nothing was
// connected to, so nothing could have been acted on.
func dialFailed(err error) bool {
	var opErr *net.OpError
	return errors.As(err, &opErr) && opErr.Op == "dial"
}

// piperFetch performs one request, retrying only while nothing answers.
func piperFetch(ctx context.Context, method, url string, reqBody []byte, deadline time.Time) (*http.Response, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	for {
		var body io.Reader
		if reqBody != nil {
			body = bytes.NewReader(reqBody)
		}
		req, err := http.NewRequestWithContext(ctx, method, url, body)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err == nil {
			return resp, nil
		}
		if method != http.MethodPost || !dialFailed(err) ||
			!time.Now().Before(deadline) || ctx.Err() != nil {
			return nil, err
		}
		time.Sleep(piperRetryInterval)
	}
}

// piperProxy forwards one request to HAL and copies the reply back.
func piperProxy(c *gin.Context, method, path string) {
	var reqBody []byte
	if method == http.MethodPost {
		reqBody, _ = io.ReadAll(c.Request.Body)
	}
	resp, err := piperFetch(c.Request.Context(), method, hal.BaseURL+path, reqBody,
		time.Now().Add(piperRetryWindow))
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("hal unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	// HAL speaks plain JSON; the web client only accepts this app's
	// {status,data,message} envelope and reads anything else as a failed
	// request.
	var payload any
	if err := json.Unmarshal(raw, &payload); err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("hal returned invalid JSON: "+err.Error()))
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		c.JSON(resp.StatusCode, serializers.ResponseError(string(raw)))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(payload))
}

func (s *Server) piperStatus(c *gin.Context)  { piperProxy(c, http.MethodGet, "/voice/piper/status") }
func (s *Server) piperInstall(c *gin.Context) { piperProxy(c, http.MethodPost, "/voice/piper/install") }
func (s *Server) piperVoice(c *gin.Context)   { piperProxy(c, http.MethodPost, "/voice/piper/voice") }
func (s *Server) piperVoiceRemove(c *gin.Context) {
	piperProxy(c, http.MethodPost, "/voice/piper/voice/remove")
}
