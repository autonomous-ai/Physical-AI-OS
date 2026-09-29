package mqtthandler

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/eclipse/paho.golang/packets"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/mqtt"
	buddyhttp "go.autonomous.ai/os/system/server/buddy/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

// captureBuddyPairReply runs the real MQTT publisher against a minimal local broker.
func captureBuddyPairReply(t *testing.T, svc *buddy.Service, kind string) map[string]json.RawMessage {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	if err := listener.(*net.TCPListener).SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	type result struct {
		publish *packets.Publish
		err     error
	}
	replies := make(chan result, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.Accept()
		if err != nil {
			replies <- result{err: err}
			return
		}
		defer conn.Close()
		if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
			replies <- result{err: err}
			return
		}
		packet, err := packets.ReadPacket(conn)
		if err != nil {
			replies <- result{err: err}
			return
		}
		if _, ok := packet.Content.(*packets.Connect); !ok {
			replies <- result{err: fmt.Errorf("expected CONNECT, got %T", packet.Content)}
			return
		}
		if _, err := (&packets.Connack{Properties: &packets.Properties{}}).WriteTo(conn); err != nil {
			replies <- result{err: err}
			return
		}
		for {
			packet, err = packets.ReadPacket(conn)
			if err != nil {
				replies <- result{err: err}
				return
			}
			if _, ok := packet.Content.(*packets.Pingreq); !ok {
				break
			}
			if _, err := (&packets.Pingresp{}).WriteTo(conn); err != nil {
				replies <- result{err: err}
				return
			}
		}
		publish, ok := packet.Content.(*packets.Publish)
		if !ok {
			replies <- result{err: fmt.Errorf("expected PUBLISH, got %T", packet.Content)}
			return
		}
		if _, err := (&packets.Puback{PacketID: publish.PacketID}).WriteTo(conn); err != nil {
			replies <- result{err: err}
			return
		}
		replies <- result{publish: publish}
		_, _ = packets.ReadPacket(conn)
	}()
	t.Cleanup(func() { <-done })

	factory, err := mqtt.ProvideFactory(mqtt.Config{
		Endpoint: "127.0.0.1",
		Port:     listener.Addr().(*net.TCPAddr).Port,
	})
	if err != nil {
		t.Fatal(err)
	}
	h := DeviceMQTTHandler{
		config:       &config.Config{DeviceID: "buddy-pair-test", FDChannel: "test/fd"},
		mqttFactory:  factory,
		buddyService: svc,
	}
	if err := h.HandleMessage("test/fa", []byte(fmt.Sprintf(`{"cmd":"data","kind":%q}`, kind))); err != nil {
		reply := <-replies
		t.Fatalf("HandleMessage: %v; broker: %v", err, reply.err)
	}
	reply := <-replies
	if reply.err != nil {
		t.Fatal(reply.err)
	}
	if reply.publish.Topic != "test/fd" || reply.publish.QoS != 1 {
		t.Fatalf("reply topic=%q QoS=%d", reply.publish.Topic, reply.publish.QoS)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(reply.publish.Payload, &envelope); err != nil {
		t.Fatal(err)
	}
	if string(envelope["type"]) != `"data"` || string(envelope["kind"]) != fmt.Sprintf("%q", kind) {
		t.Fatalf("unexpected envelope: %s", reply.publish.Payload)
	}
	return envelope
}

func TestBuddyPairStartMQTTConfirmHTTP(t *testing.T) {
	t.Chdir(t.TempDir())
	svc, err := buddy.ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	envelope := captureBuddyPairReply(t, svc, domain.KindBuddyPairStart)
	if string(envelope["status"]) != `"success"` {
		t.Fatalf("unexpected status: %s", envelope["status"])
	}
	var data struct {
		Code      string `json:"code"`
		ExpiresIn int    `json:"expires_in"`
	}
	if err := json.Unmarshal(envelope["data"], &data); err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9]{6}$`).MatchString(data.Code) || data.ExpiresIn != 60 {
		t.Fatalf("unexpected pairing data: %+v", data)
	}

	h := buddyhttp.ProvideBuddyHandler(&config.Config{}, svc)
	router := gin.New()
	router.POST("/api/buddy/pair/confirm", h.PairConfirm)
	confirm := func() *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/buddy/pair/confirm", strings.NewReader(fmt.Sprintf(`{"code":%q,"name":"Test Mac"}`, data.Code)))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, request)
		return response
	}
	response := confirm()
	if response.Code != http.StatusOK {
		t.Fatalf("HTTP confirmation: %d %s", response.Code, response.Body.String())
	}
	var paired struct {
		Status int `json:"status"`
		Data   struct {
			Token string `json:"token"`
		} `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &paired); err != nil {
		t.Fatal(err)
	}
	if paired.Status != 1 || paired.Data.Token == "" || svc.ValidateToken(paired.Data.Token) == nil {
		t.Fatal("HTTP confirmation did not issue a valid buddy token")
	}
	if response := confirm(); response.Code != http.StatusBadRequest {
		t.Fatalf("reused pairing code accepted: %d", response.Code)
	}
}

func TestBuddyPairStartMQTTUnavailable(t *testing.T) {
	envelope := captureBuddyPairReply(t, nil, domain.KindBuddyPairStart)
	if string(envelope["status"]) != `"failure"` || string(envelope["error"]) != `"buddy service unavailable"` {
		t.Fatalf("unexpected failure reply: %v", envelope)
	}
	if data := envelope["data"]; len(data) != 0 && string(data) != "null" {
		t.Fatalf("unavailable service returned pairing data: %s", data)
	}
}

func TestBuddyPairRevokeMQTT(t *testing.T) {
	t.Chdir(t.TempDir())
	svc, err := buddy.ProvideService()
	if err != nil {
		t.Fatal(err)
	}
	code, _ := svc.IssuePairingCode()
	paired, err := svc.ConfirmPairing("Test Mac", "", "", code)
	if err != nil {
		t.Fatal(err)
	}

	// Register a real WebSocket so revocation must close the peer connection.
	registered := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade: %v", err)
			close(registered)
			return
		}
		svc.RegisterConnection(conn)
		close(registered)
	}))
	defer server.Close()
	peer, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer peer.Close()
	<-registered
	if !svc.Connected() {
		t.Fatal("buddy did not connect")
	}

	for i := 0; i < 2; i++ {
		envelope := captureBuddyPairReply(t, svc, domain.KindBuddyPairRevoke)
		if string(envelope["status"]) != `"success"` || string(envelope["data"]) != `{"revoked":true}` {
			t.Fatalf("unexpected revoke response: %v", envelope)
		}
		if svc.Paired() != nil || svc.ValidateToken(paired.Token) != nil || svc.Connected() {
			t.Fatal("revocation left pairing, token, or connection active")
		}
		restored, err := buddy.ProvideService()
		if err != nil {
			t.Fatal(err)
		}
		if restored.Paired() != nil {
			t.Fatal("revocation was not persisted")
		}
	}
	if err := peer.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := peer.ReadMessage(); err == nil {
		t.Fatal("revocation did not close WebSocket")
	} else if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
		t.Fatal("WebSocket timed out instead of closing")
	}
}

func TestBuddyPairRevokeMQTTFailure(t *testing.T) {
	t.Run("unavailable", func(t *testing.T) {
		envelope := captureBuddyPairReply(t, nil, domain.KindBuddyPairRevoke)
		if string(envelope["status"]) != `"failure"` || string(envelope["error"]) != `"buddy service unavailable"` {
			t.Fatalf("unexpected failure response: %v", envelope)
		}
	})
	t.Run("persist", func(t *testing.T) {
		t.Chdir(t.TempDir())
		svc, err := buddy.ProvideService()
		if err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(buddy.BuddiesFilePath, 0o755); err != nil {
			t.Fatal(err)
		}
		envelope := captureBuddyPairReply(t, svc, domain.KindBuddyPairRevoke)
		if string(envelope["status"]) != `"failure"` || !strings.Contains(string(envelope["error"]), "clear store:") {
			t.Fatalf("unexpected persistence failure response: %v", envelope)
		}
		if data := envelope["data"]; len(data) != 0 && string(data) != "null" {
			t.Fatalf("failed revoke returned success data: %s", data)
		}
	})
}
