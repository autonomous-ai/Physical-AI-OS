package session

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/config"
)

// CookieName is the browser cookie that carries the HMAC-signed session token
// after a successful POST /api/login.
const CookieName = "os_session"

// TTL is how long an issued session stays valid.
const TTL = 30 * 24 * time.Hour

// ensureSecret writes a random 32-byte hex secret into cfg.SessionSecret when
// it's empty so freshly upgraded devices auto-bootstrap signing material.
func ensureSecret(cfg *config.Config) ([]byte, error) {
	if cfg.SessionSecret != "" {
		key, err := hex.DecodeString(cfg.SessionSecret)
		if err == nil && len(key) >= 16 {
			return key, nil
		}
		slog.Warn("session secret malformed, regenerating", "component", "session", "error", err)
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return nil, fmt.Errorf("rand: %w", err)
	}
	cfg.SessionSecret = hex.EncodeToString(buf)
	if err := cfg.Save(); err != nil {
		return nil, fmt.Errorf("persist session secret: %w", err)
	}
	return buf, nil
}

// sign returns a stateless `<exp>.<sig>` token.
func sign(secret []byte, expiresAt time.Time) string {
	exp := strconv.FormatInt(expiresAt.Unix(), 10)
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(exp))
	sig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return exp + "." + sig
}

// verify returns nil iff token is well-formed, HMAC matches under secret, and
// the embedded expiry is still in the future.
func verify(secret []byte, token string, now time.Time) error {
	parts := strings.SplitN(token, ".", 2)
	if len(parts) != 2 {
		return fmt.Errorf("malformed token")
	}
	exp, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return fmt.Errorf("malformed exp")
	}
	if now.Unix() >= exp {
		return fmt.Errorf("expired")
	}
	expected := hmac.New(sha256.New, secret)
	expected.Write([]byte(parts[0]))
	want := base64.RawURLEncoding.EncodeToString(expected.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(parts[1]), []byte(want)) != 1 {
		return fmt.Errorf("bad signature")
	}
	return nil
}

// Issue signs a fresh session token and writes Set-Cookie.
func Issue(c *gin.Context, cfg *config.Config) error {
	secret, err := ensureSecret(cfg)
	if err != nil {
		return err
	}
	expiresAt := time.Now().Add(TTL)
	token := sign(secret, expiresAt)
	c.Set("_sess_tok", token)
	maxAge := int(TTL / time.Second)
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     CookieName,
		Value:    token,
		Path:     "/",
		MaxAge:   maxAge,
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	})
	return nil
}

// Clear expires the cookie.
func Clear(c *gin.Context) {
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	})
}

// LatestToken returns the token issued by Issue in this request.
func LatestToken(c *gin.Context) string {
	v, _ := c.Get("_sess_tok")
	s, _ := v.(string)
	return s
}

// VerifyToken checks a raw token string (from Bearer header) against the
// current session secret. Same validation as the cookie path.
func VerifyToken(token string, cfg *config.Config) bool {
	if token == "" || cfg.SessionSecret == "" {
		return false
	}
	secret, err := hex.DecodeString(cfg.SessionSecret)
	if err != nil {
		return false
	}
	return verify(secret, token, time.Now()) == nil
}

// HasValid returns true if the request carries a os_session cookie that
// verifies under the current secret and hasn't expired.
func HasValid(c *gin.Context, cfg *config.Config) bool {
	if cfg.SessionSecret == "" {
		return false
	}
	cookie, err := c.Request.Cookie(CookieName)
	if err != nil || cookie.Value == "" {
		return false
	}
	secret, err := hex.DecodeString(cfg.SessionSecret)
	if err != nil {
		return false
	}
	return verify(secret, cookie.Value, time.Now()) == nil
}
