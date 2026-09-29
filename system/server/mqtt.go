package server

import (
	"context"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/server/config"
)

// restartMQTT stops the current MQTT client and starts a new one (e.g. when backend pushes new MQTT config).
func (s *Server) restartMQTT() {
	s.stopMQTT()
	if s.mqttFactory != nil {
		s.mqttFactory.UpdateConfig(config.ProvideMQTTConfig(s.config))
	}
	s.startMQTT()
}

// startMQTT creates a client from the factory, subscribes to the topic, and connects. Idempotent if already running.
func (s *Server) startMQTT() {
	s.mqttMu.Lock()
	if s.mqttClient != nil {
		s.mqttMu.Unlock()
		return
	}
	if s.mqttFactory == nil {
		s.mqttMu.Unlock()
		return
	}
	// Client IDs derive from device_id: two processes sharing a config would
	// fight over one broker session.
	if !syspath.BackendUplink() {
		s.mqttMu.Unlock()
		slog.Info("backend uplink off — mqtt not started", "component", "mqtt")
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	client := s.mqttFactory.GetClient("os-server-" + s.config.DeviceID)
	slog.Info("subscribing to FA channel", "component", "mqtt", "topic", s.config.FAChannel)
	client.Subscribe(s.config.FAChannel, 1, func(topic string, payload []byte) {
		slog.Debug("message received", "component", "mqtt", "topic", topic, "payload", string(payload))
		s.deviceMQTTHandler.HandleMessage(topic, payload)
	})
	s.mqttClient = client
	s.mqttCancel = cancel
	s.mqttMu.Unlock()

	safego.Go("mqtt", func() {
		if err := client.Connect(ctx); err != nil && ctx.Err() == nil {
			slog.Error("connect failed", "component", "mqtt", "error", err)
		}
	})

	// Re-publishing is safe: the backend collapses replays on intent_id.
	safego.Go("schedule-intent-flush", func() {
		ticker := time.NewTicker(scheduleIntentFlushInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.deviceMQTTHandler.FlushScheduleIntents()
			}
		}
	})
}

// scheduleIntentFlushInterval is how often queued device-originated schedule
// changes are retried.
const scheduleIntentFlushInterval = 30 * time.Second

// stopMQTT disconnects and clears the MQTT client. Safe to call when not connected.
func (s *Server) stopMQTT() {
	s.mqttMu.Lock()
	client := s.mqttClient
	cancel := s.mqttCancel
	s.mqttClient = nil
	s.mqttCancel = nil
	s.mqttMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if client != nil {
		_ = client.Close()
	}
}
