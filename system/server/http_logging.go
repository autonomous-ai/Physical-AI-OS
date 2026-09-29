package server

import (
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
	return gin.CustomRecoveryWithWriter(nil, func(c *gin.Context, _ any) {
		fmt.Fprintf(out, "[HTTP] panic recovered\n%s", debug.Stack())
		c.AbortWithStatus(http.StatusInternalServerError)
	})
}
