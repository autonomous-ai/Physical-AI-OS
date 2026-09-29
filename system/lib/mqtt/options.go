// Package mqtt provides an MQTT client with auto-connect and reconnect (Eclipse Paho autopaho).
package mqtt

import (
	"fmt"
	"net/url"
	"strings"
	"time"
)

// DefaultKeepAlive is the default keepalive in seconds (spec maximum).
const DefaultKeepAlive = 65535

// DefaultConnectTimeout is the default connection timeout.
const DefaultConnectTimeout = 30 * time.Second

// DefaultPort is the default MQTT port when Port is 0.
const DefaultPort = 1883

// Options configures the MQTT client. Endpoint is required to enable MQTT.
type Options struct {
	// Endpoint is the broker host (domain or IP only); empty disables MQTT.
	Endpoint string
	// Port is the broker port (e.g. 1883, 8883). 0 uses DefaultPort.
	Port int
	// ClientID defaults to a generated value when empty.
	ClientID string
	Username string
	Password string
	// KeepAlive is in seconds; 0 uses DefaultKeepAlive.
	KeepAlive uint16
	// ConnectTimeout bounds the initial connection; 0 uses DefaultConnectTimeout.
	ConnectTimeout time.Duration
}

// ServerURL returns mqtt://host:port for Paho; call Validate first.
func (o *Options) ServerURL() (*url.URL, error) {
	host := strings.TrimSpace(o.Endpoint)
	if host == "" {
		return nil, fmt.Errorf("mqtt: endpoint is required")
	}
	port := o.Port
	if port == 0 {
		port = DefaultPort
	}
	u := &url.URL{
		Scheme: "mqtt",
		Host:   fmt.Sprintf("%s:%d", host, port),
	}
	return u, nil
}
