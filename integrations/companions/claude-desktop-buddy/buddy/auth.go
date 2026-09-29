package buddy

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"os"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"
)

// Authenticator validates the admin Bearer token against the OS server config.json (admin_password_hash or llm_api_key).
// It fails closed: with no credential configured, Authorize always denies.
type Authenticator struct {
	path string

	mu       sync.Mutex
	modTime  time.Time
	hash     string // bcrypt(admin password)
	apiKey   string // llm_api_key machine token
	verified string // last plaintext that matched the current hash (bcrypt cache)
}

// NewAuthenticator reads credentials from the given OS server config.json path.
func NewAuthenticator(osConfigPath string) *Authenticator {
	a := &Authenticator{path: osConfigPath}
	a.refresh()
	if a.hash == "" && a.apiKey == "" {
		log.Printf("[auth] WARN: no admin_password_hash or llm_api_key in %s — all LAN endpoints will return 401 until set", osConfigPath)
	}
	return a
}

// refresh reloads credentials when config.json changes, clearing the plaintext cache.
func (a *Authenticator) refresh() {
	fi, err := os.Stat(a.path)
	if err != nil {
		return // keep last-known values if the file is briefly unreadable
	}
	if fi.ModTime().Equal(a.modTime) {
		return
	}
	data, err := os.ReadFile(a.path)
	if err != nil {
		return
	}
	var c struct {
		AdminPasswordHash string `json:"admin_password_hash"`
		LLMAPIKey         string `json:"llm_api_key"`
	}
	if err := json.Unmarshal(data, &c); err != nil {
		log.Printf("[auth] config parse error: %v (keeping previous credentials)", err)
		return
	}
	a.modTime = fi.ModTime()
	a.hash = c.AdminPasswordHash
	a.apiKey = c.LLMAPIKey
	a.verified = ""
}

// Authorize reports whether secret is the admin password or API key; the hot path is a constant-time compare against the cached plaintext.
func (a *Authenticator) Authorize(secret string) bool {
	if secret == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.refresh()

	if a.verified != "" && subtle.ConstantTimeCompare([]byte(secret), []byte(a.verified)) == 1 {
		return true
	}
	if a.apiKey != "" && subtle.ConstantTimeCompare([]byte(secret), []byte(a.apiKey)) == 1 {
		return true
	}
	if a.hash != "" && bcrypt.CompareHashAndPassword([]byte(a.hash), []byte(secret)) == nil {
		a.verified = secret
		return true
	}
	return false
}
