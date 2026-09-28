package device

import (
	"testing"
	"time"
)

func TestConfigWiFiReconnectLeavesResponseGrace(t *testing.T) {
	type attempt struct {
		ssid, password string
		at             time.Time
	}
	called := make(chan attempt, 1)
	started := time.Now()
	timer := scheduleConfigWiFiReconnect(updateChanges{wifi: true, newSSID: "new-network", newPassword: "saved-password"}, func(ssid, password string) (bool, error) {
		called <- attempt{ssid, password, time.Now()}
		return true, nil
	})
	if timer == nil {
		t.Fatal("no reconnect scheduled")
	}
	defer timer.Stop()
	select {
	case <-called:
		t.Fatal("Wi-Fi disconnected before the response grace")
	case <-time.After(100 * time.Millisecond):
	}
	select {
	case got := <-called:
		if got.at.Sub(started) < configWiFiResponseGrace {
			t.Fatal("reconnected too soon")
		}
		if got.ssid != "new-network" || got.password != "saved-password" {
			t.Fatal("lost saved credentials")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("reconnect never ran")
	}
}

func TestConfigWithoutWiFiChangeDoesNotScheduleReconnect(t *testing.T) {
	timer := scheduleConfigWiFiReconnect(updateChanges{}, func(string, string) (bool, error) {
		t.Error("non-Wi-Fi save reconnected network")
		return true, nil
	})
	if timer != nil {
		timer.Stop()
		t.Fatal("unexpected reconnect timer")
	}
}
