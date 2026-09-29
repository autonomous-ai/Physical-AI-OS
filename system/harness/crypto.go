package harness

import (
	"bytes"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode"

	"github.com/gtank/ristretto255"
	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/hkdf"
)

func randomBytes(n int) ([]byte, error) { b := make([]byte, n); _, err := rand.Read(b); return b, err }
func b64(b []byte) string               { return base64.StdEncoding.EncodeToString(b) }
func decode(value any, n int) ([]byte, error) {
	s, ok := value.(string)
	if !ok {
		return nil, errors.New("invalid encoded field")
	}
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil || (n >= 0 && len(b) != n) || b64(b) != s {
		return nil, errors.New("invalid encoded field")
	}
	return b, nil
}
func lv(parts ...[]byte) []byte {
	var out bytes.Buffer
	for _, p := range parts {
		_ = binary.Write(&out, binary.BigEndian, uint32(len(p)))
		out.Write(p)
	}
	return out.Bytes()
}
func derive(secret, salt []byte, info string, n int) []byte {
	out := make([]byte, n)
	_, _ = io.ReadFull(hkdf.New(sha256.New, secret, salt, []byte(info)), out)
	return out
}
func mac(key, msg []byte) []byte { h := hmac.New(sha256.New, key); h.Write(msg); return h.Sum(nil) }
func digest512(b []byte) []byte  { h := sha512.Sum512(b); return h[:] }
func seal(key []byte, n uint64, aad, plain []byte) ([]byte, error) {
	a, err := chacha20poly1305.New(key)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, 12)
	binary.BigEndian.PutUint64(nonce, n)
	return a.Seal(nil, nonce, plain, aad), nil
}
func openSealed(key []byte, n uint64, aad, cipher []byte) ([]byte, error) {
	a, err := chacha20poly1305.New(key)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, 12)
	binary.BigEndian.PutUint64(nonce, n)
	return a.Open(nil, nonce, cipher, aad)
}

func normalizeCode(code string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) || r == '-' || r == '·' || r == '_' {
			return -1
		}
		switch r {
		case 'I', 'L':
			return '1'
		case 'O':
			return '0'
		case 'U':
			return 'V'
		}
		return r
	}, strings.ToUpper(code))
}

// expand_message_xmd(SHA-512), L=64, matches noble hashToRistretto255's DST.
func cpaceGenerator(code string, pairID []byte, ci string) *ristretto255.Element {
	const dst = "e2e-cpace-ristretto255-v1"
	msg := lv([]byte(dst), []byte(normalizeCode(code)), pairID, []byte(ci))
	d := append([]byte(dst), byte(len(dst)))
	b0 := digest512(bytes.Join([][]byte{make([]byte, 128), msg, {0, 64}, {0}, d}, nil))
	b1 := digest512(bytes.Join([][]byte{b0, {1}, d}, nil))
	return ristretto255.NewElement().FromUniformBytes(b1)
}

type pake struct {
	yb, th, key, kcA, kcB []byte
}

func newPakeContext(code string, pairID, ya []byte, ci string) (*pake, error) {
	random, err := randomBytes(64)
	if err != nil {
		return nil, err
	}
	y := ristretto255.NewScalar().FromUniformBytes(random)
	if y.Equal(ristretto255.NewScalar()) == 1 {
		return nil, errors.New("invalid random scalar")
	}
	peer := ristretto255.NewElement()
	if err = peer.Decode(ya); err != nil {
		return nil, fmt.Errorf("pair point: %w", err)
	}
	shared := ristretto255.NewElement().ScalarMult(y, peer)
	if shared.Equal(ristretto255.NewElement()) == 1 {
		return nil, errors.New("identity pair point")
	}
	yb := ristretto255.NewElement().ScalarMult(y, cpaceGenerator(code, pairID, ci)).Encode(nil)
	a, b := lv(ya, []byte("a")), lv(yb, []byte("b"))
	isk := digest512(lv([]byte("e2e-cpace-ristretto255-v1_ISK"), pairID, shared.Encode(nil), a, b))
	th := digest512(lv(pairID, []byte(ci), a, b))
	kc := derive(isk, []byte(ci), "e2e-kc-v1", 64)
	return &pake{yb: yb, th: th, key: derive(isk, []byte(ci), "e2e-id-v1", 32), kcA: kc[:32], kcB: kc[32:]}, nil
}
func (p *pake) identity(frame Frame) ([]byte, error) {
	tag, err := decode(frame["mac"], 32)
	if err != nil || !hmac.Equal(tag, mac(p.kcA, p.th)) {
		return nil, errors.New("pair code mismatch")
	}
	cipher, err := decode(frame["enc"], -1)
	if err != nil {
		return nil, err
	}
	raw, err := openSealed(p.key, 3, []byte("e2e-id"), cipher)
	if err != nil {
		return nil, errors.New("pair identity authentication failed")
	}
	var v Frame
	if json.Unmarshal(raw, &v) != nil {
		return nil, errors.New("invalid pair identity")
	}
	pub, err := decode(v["id"], 32)
	if err != nil {
		return nil, err
	}
	sig, err := decode(v["sig"], 64)
	if err != nil || !ed25519.Verify(pub, append([]byte("e2e-pair-bind"), p.th...), sig) {
		return nil, errors.New("invalid pair signature")
	}
	return pub, nil
}
func (p *pake) identityReply(key ed25519.PrivateKey) (string, error) {
	raw, err := json.Marshal(Frame{"id": b64(key.Public().(ed25519.PublicKey)), "sig": b64(ed25519.Sign(key, append([]byte("e2e-pair-bind"), p.th...)))})
	if err != nil {
		return "", err
	}
	cipher, err := seal(p.key, 4, []byte("e2e-id"), raw)
	return b64(cipher), err
}
