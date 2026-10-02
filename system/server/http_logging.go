package server

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"runtime/debug"
	"strings"

	"github.com/gin-gonic/gin"
)

// Do not log any query values: legacy auth and provisioning links may carry
// credentials, and a secret-name allowlist would miss future additions.
func credentialSafeLogger(out io.Writer) gin.HandlerFunc {
	return gin.LoggerWithConfig(gin.LoggerConfig{
		Output: out,
		Formatter: func(p gin.LogFormatterParams) string {
			path, _, _ := strings.Cut(p.Path, "?")
			return fmt.Sprintf("[HTTP] %s | %d | %s | %s | %s %q\n",
				p.TimeStamp.Format("2006/01/02 - 15:04:05"), p.StatusCode,
				p.Latency, p.ClientIP, p.Method, path)
		},
	})
}

func credentialSafeRecovery(out io.Writer) gin.HandlerFunc {
	// Gin's default recovery dumps the URL, headers and panic value. Any of
	// these may contain credentials. Retain a stack trace without that data.
	return gin.CustomRecoveryWithWriter(nil, func(c *gin.Context, recovered any) {
		// The reverse proxy panics with ErrAbortHandler when a streamed
		// response is cut short (browser closed an SSE stream, HAL restarted).
		// That is a normal disconnect, not a bug worth a stack trace.
		if err, ok := recovered.(error); ok && errors.Is(err, http.ErrAbortHandler) {
			c.Abort()
			return
		}
		fmt.Fprintf(out, "[HTTP] panic recovered\n%s", debug.Stack())
		c.AbortWithStatus(http.StatusInternalServerError)
	})
}
