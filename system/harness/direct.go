package harness

import (
	"context"
	"crypto/ecdh"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// DirectChannel carries the original Harness device protocol on a socket opened
// by the paired computer. Exactly one owner reads during pairing and sessions.
type DirectChannel struct {
	ws        *websocket.Conn
	writeMu   sync.Mutex
	machineID string
	crypto    *deviceSessionCrypto
}

// ErrRevoked means the paired computer no longer trusts this device; the pin is dropped.
var ErrRevoked = errors.New("Harness computer revoked this device")

func (c *DirectChannel) Close() error { return c.ws.Close() }
func (c *DirectChannel) Write(frame Frame) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	_ = c.ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return c.ws.WriteJSON(frame)
}
func (c *DirectChannel) Read() (Frame, error) {
	var f Frame
	if err := c.ws.ReadJSON(&f); err != nil {
		return nil, err
	}
	if f == nil {
		return nil, errors.New("invalid session frame")
	}
	return f, nil
}
func payloadOf(f Frame) Frame {
	switch p := f["payload"].(type) {
	case Frame:
		return p
	case map[string]any:
		return Frame(p)
	default:
		return Frame{}
	}
}

// Pair reuses the device responder of the original CPace exchange. onPin persists
// the authenticated round-three pin; losing round five is recoverable via hello.
func (c *DirectChannel) Pair(ctx context.Context, identity ed25519.PrivateKey, code string, onPin func([]byte) error) error {
	if c.machineID == "" {
		return errors.New("missing Harness machine identity")
	}
	stop := context.AfterFunc(ctx, func() { c.ws.Close() })
	defer stop()
	id, err := randomBytes(16)
	if err != nil {
		return err
	}
	pairID := b64(id)
	if err = c.Write(Frame{"type": "e2e_pair_intent", "payload": Frame{"pairId": pairID, "label": "Autonomous Device", "role": "device"}}); err != nil {
		return err
	}
	var state *pake
	for {
		frame, err := c.Read()
		if err != nil {
			return err
		}
		p := payloadOf(frame)
		switch stringField(frame, "type") {
		case "e2e_pair_intent_result":
			if p["accepted"] != true {
				return fmt.Errorf("pair intent: %v", p["error"])
			}
		case "e2e_pake":
			if stringField(p, "pairId") != pairID {
				continue
			}
			switch number(p, "round") {
			case 1:
				ya, e := decode(p["ya"], 32)
				if e != nil {
					return e
				}
				state, e = newPakeContext(code, id, ya, "autonomous-e2e-pair|agent:"+c.machineID+"|a:adapter|b:device")
				if e != nil {
					return e
				}
				if e = c.Write(Frame{"type": "e2e_pake", "payload": Frame{"pairId": pairID, "round": 2, "yb": b64(state.yb), "mac": b64(mac(state.kcB, state.th))}}); e != nil {
					return e
				}
			case 3:
				if state == nil {
					return errors.New("unexpected pairing round")
				}
				pub, e := state.identity(p)
				if e != nil {
					return e
				}
				if onPin == nil {
					return errors.New("pairing requires durable pin storage")
				}
				if e = onPin(pub); e != nil {
					return e
				}
				enc, e := state.identityReply(identity)
				if e != nil {
					return e
				}
				if e = c.Write(Frame{"type": "e2e_pake", "payload": Frame{"pairId": pairID, "round": 4, "enc": enc}}); e != nil {
					return e
				}
			case 5:
				if p["ok"] != true {
					return fmt.Errorf("pairing failed: %v", p["error"])
				}
				return nil
			}
		}
	}
}
func (c *DirectChannel) Establish(ctx context.Context, identity ed25519.PrivateKey, pinned ed25519.PublicKey) error {
	if c.machineID == "" {
		return errors.New("missing Harness machine identity")
	}
	crypto, err := newDeviceSessionCrypto(c.machineID, identity, pinned)
	if err != nil {
		return err
	}
	stop := context.AfterFunc(ctx, func() { c.ws.Close() })
	defer stop()
	if err = c.Write(crypto.hello()); err != nil {
		return err
	}
	for {
		f, err := c.Read()
		if err != nil {
			return err
		}
		switch stringField(f, "type") {
		case "e2e_welcome":
			if err = crypto.welcome(payloadOf(f)); err != nil {
				return err
			}
			c.crypto = crypto
			return nil
		case "e2e_denied":
			return ErrRevoked
		}
	}
}
func (c *DirectChannel) SendEncrypted(frame Frame) error {
	if c.crypto == nil {
		return errors.New("Harness E2EE is not established")
	}
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	sealed, err := c.crypto.wrap(frame)
	if err != nil {
		return err
	}
	_ = c.ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return c.ws.WriteJSON(sealed)
}
func (c *DirectChannel) ReadDecrypted() (Frame, error) {
	for {
		f, err := c.Read()
		if err != nil {
			return nil, err
		}
		if c.crypto == nil {
			return nil, errors.New("Harness E2EE is not established")
		}
		switch stringField(f, "type") {
		case "autonomous_device_result", "autonomous_device_event", "commander_event", "commander_question":
			if _, wrapped := payloadOf(f)["__e2e"]; !wrapped {
				return nil, errors.New("unencrypted Harness data frame")
			}
		}
		if stringField(f, "type") == "e2e_rekey" {
			if err = c.crypto.rekey(payloadOf(f)); err != nil {
				return nil, err
			}
			continue
		}
		return c.crypto.unwrap(f)
	}
}

type sessionReplay struct {
	highest uint64
	seen    map[uint64]bool
}

func (r *sessionReplay) allows(n uint64) bool {
	return !r.seen[n] && (r.seen == nil || n > r.highest || r.highest-n < 4096)
}
func (r *sessionReplay) commit(n uint64) {
	if r.seen == nil {
		r.seen = make(map[uint64]bool)
	}
	r.seen[n] = true
	if n > r.highest {
		r.highest = n
	}
	for v := range r.seen {
		if r.highest >= v && r.highest-v >= 4096 {
			delete(r.seen, v)
		}
	}
}

type deviceSessionCrypto struct {
	machineID                   string
	identity                    ed25519.PrivateKey
	peer                        ed25519.PublicKey
	eph                         *ecdh.PrivateKey
	c2s, s2c, group             []byte
	epoch                       string
	tx                          uint64
	pairwiseReplay, groupReplay sessionReplay
}

func newDeviceSessionCrypto(machine string, identity ed25519.PrivateKey, pinned ed25519.PublicKey) (*deviceSessionCrypto, error) {
	if len(identity) != ed25519.PrivateKeySize || len(pinned) != ed25519.PublicKeySize {
		return nil, errors.New("invalid session identity")
	}
	eph, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	return &deviceSessionCrypto{machineID: machine, identity: identity, peer: pinned, eph: eph}, nil
}
func (c *deviceSessionCrypto) hello() Frame {
	pub := c.eph.PublicKey().Bytes()
	return Frame{"type": "e2e_hello", "payload": Frame{"identityPub": b64(c.identity.Public().(ed25519.PublicKey)), "ephPub": b64(pub), "sig": b64(ed25519.Sign(c.identity, lv([]byte("e2e-hello-v1"), []byte(c.machineID), pub)))}}
}
func (c *deviceSessionCrypto) welcome(p Frame) error {
	if stringField(p, "webEphPub") != b64(c.eph.PublicKey().Bytes()) {
		return errors.New("welcome does not address this session")
	}
	pub, err := decode(p["ephPub"], 32)
	if err != nil {
		return err
	}
	sig, err := decode(p["sig"], 64)
	if err != nil {
		return err
	}
	if !ed25519.Verify(c.peer, lv([]byte("e2e-welcome-v1"), []byte(c.machineID), c.eph.PublicKey().Bytes(), pub), sig) {
		return errors.New("invalid Harness welcome signature")
	}
	remote, err := ecdh.X25519().NewPublicKey(pub)
	if err != nil {
		return err
	}
	shared, err := c.eph.ECDH(remote)
	if err != nil {
		return err
	}
	keys := derive(shared, lv([]byte(c.machineID), c.eph.PublicKey().Bytes(), pub), "e2e-sess-v1", 64)
	cipher, err := decode(p["enc"], -1)
	if err != nil {
		return err
	}
	plain, err := openSealed(keys[32:], 0, []byte("e2e-welcome"), cipher)
	if err != nil {
		return err
	}
	var initial Frame
	if json.Unmarshal(plain, &initial) != nil {
		return errors.New("invalid group key welcome")
	}
	group, err := decode(initial["groupKey"], 32)
	if err != nil {
		return err
	}
	epoch := stringField(initial, "epoch")
	if epoch == "" {
		return errors.New("missing group epoch")
	}
	c.c2s, c.s2c, c.group, c.epoch = keys[:32], keys[32:], group, epoch
	c.pairwiseReplay.commit(0)
	return nil
}
func (c *deviceSessionCrypto) rekey(p Frame) error {
	n, ok := sessionCounter(p["n"])
	if !ok || !c.pairwiseReplay.allows(n) {
		return errors.New("replayed group rekey")
	}
	cipher, err := decode(p["enc"], -1)
	if err != nil {
		return err
	}
	plain, err := openSealed(c.s2c, n, []byte("e2e-rekey"), cipher)
	if err != nil {
		return err
	}
	var next Frame
	if json.Unmarshal(plain, &next) != nil {
		return errors.New("invalid group rekey")
	}
	key, err := decode(next["groupKey"], 32)
	if err != nil {
		return err
	}
	epoch := stringField(next, "epoch")
	if epoch == "" {
		return errors.New("missing group epoch")
	}
	c.pairwiseReplay.commit(n)
	c.group = key
	c.epoch = epoch
	c.groupReplay = sessionReplay{}
	return nil
}
func sessionCounter(value any) (uint64, bool) {
	switch n := value.(type) {
	case float64:
		if n >= 0 && n <= 9007199254740991 && n == float64(uint64(n)) {
			return uint64(n), true
		}
	case uint64:
		if n <= 9007199254740991 {
			return n, true
		}
	case int:
		if n >= 0 {
			return uint64(n), true
		}
	}
	return 0, false
}
func (c *deviceSessionCrypto) wrap(f Frame) (Frame, error) {
	if len(c.c2s) != 32 {
		return nil, errors.New("no session key")
	}
	if c.tx >= 9007199254740991 {
		return nil, errors.New("session counter exhausted")
	}
	kind := stringField(f, "type")
	if kind == "" {
		return nil, errors.New("missing frame type")
	}
	raw, err := json.Marshal(f["payload"])
	if err != nil {
		return nil, err
	}
	cipher, err := seal(c.c2s, c.tx, []byte("1|"+kind+"||p|"), raw)
	if err != nil {
		return nil, err
	}
	out := Frame{}
	for k, v := range f {
		out[k] = v
	}
	out["payload"] = Frame{"__e2e": Frame{"v": 1, "k": "p", "n": c.tx, "ct": b64(cipher)}}
	c.tx++
	return out, nil
}
func (c *deviceSessionCrypto) unwrap(f Frame) (Frame, error) {
	p := payloadOf(f)
	value, wrapped := p["__e2e"]
	if !wrapped {
		return f, nil
	}
	var env Frame
	switch v := value.(type) {
	case Frame:
		env = v
	case map[string]any:
		env = Frame(v)
	default:
		return nil, errors.New("invalid E2EE envelope")
	}
	v, ok := sessionCounter(env["v"])
	if !ok || v != 1 {
		return nil, errors.New("invalid E2EE version")
	}
	n, ok := sessionCounter(env["n"])
	if !ok {
		return nil, errors.New("invalid E2EE counter")
	}
	kind := stringField(env, "k")
	epoch := stringField(env, "epoch")
	key := c.s2c
	window := &c.pairwiseReplay
	if kind == "g" {
		if epoch != c.epoch {
			return nil, errors.New("unknown group epoch")
		}
		key = c.group
		window = &c.groupReplay
	} else if kind != "p" {
		return nil, errors.New("invalid E2EE key kind")
	}
	if !window.allows(n) {
		return nil, errors.New("replayed session record")
	}
	cipher, err := decode(env["ct"], -1)
	if err != nil {
		return nil, err
	}
	aad := []byte("1|" + stringField(f, "type") + "|" + stringField(f, "dbSessionId") + "|" + kind + "|" + epoch)
	plain, err := openSealed(key, n, aad, cipher)
	if err != nil {
		return nil, err
	}
	var decoded any
	if json.Unmarshal(plain, &decoded) != nil {
		return nil, errors.New("invalid decrypted session payload")
	}
	window.commit(n)
	out := Frame{}
	for k, v := range f {
		out[k] = v
	}
	out["payload"] = decoded
	return out, nil
}
