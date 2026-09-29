package buddy

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"sync"

	"tinygo.org/x/bluetooth"
)

var adapter = bluetooth.DefaultAdapter

// Nordic UART Service UUIDs
var (
	nusServiceUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x01, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
	nusRXUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x02, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
	nusTXUUID = bluetooth.NewUUID([16]byte{
		0x6e, 0x40, 0x00, 0x03, 0xb5, 0xa3, 0xf3, 0x93,
		0xe0, 0xa9, 0xe5, 0x0e, 0x24, 0xdc, 0xca, 0x9e,
	})
)

// BLEServer manages the Nordic UART GATT server.
// Concurrency: rxMu guards rxBuf, sendMu guards txChar writes, and one processor goroutine runs onMessage/onConnect sequentially.
type BLEServer struct {
	rxMu       sync.Mutex // also guards connected/links/lastUp/clientAddr (shared with the D-Bus connect-handler dispatch)
	sendMu     sync.Mutex
	deviceName string
	txChar     bluetooth.Characteristic
	onMessage  func([]byte)
	onConnect  func(connected bool)
	rxBuf      bytes.Buffer
	connected  bool
	links      map[string]bool // transport-level connections by address (any BT device on the adapter)
	lastUp     string          // address of the most recent transport connect
	clientAddr string          // address attributed to the active NUS client (Claude Desktop)
	msgCh      chan []byte
	evtCh      chan bool
}

func NewBLEServer(deviceName string, onMessage func([]byte), onConnect func(connected bool)) *BLEServer {
	s := &BLEServer{
		deviceName: deviceName,
		onMessage:  onMessage,
		onConnect:  onConnect,
		links:      make(map[string]bool),
		msgCh:      make(chan []byte, 64),
		evtCh:      make(chan bool, 4),
	}
	go s.processor()
	return s
}

// processor runs callbacks on one goroutine so onMessage and onConnect never race.
func (s *BLEServer) processor() {
	for {
		select {
		case line := <-s.msgCh:
			if s.onMessage != nil {
				s.onMessage(line)
			}
		case connected := <-s.evtCh:
			if s.onConnect != nil {
				s.onConnect(connected)
			}
		}
	}
}

// Start initializes the BLE adapter and begins advertising.
func (s *BLEServer) Start() error {
	log.Println("[ble] enabling adapter...")
	if err := adapter.Enable(); err != nil {
		return err
	}

	// tinygo leaves BlueZ at the 1.28 s default adv interval, too slow for macOS scans; tune via debugfs before adv.Start().
	tuneAdvIntervals()

	// Transport connects fire for any device on the radio, so they only track links; "connected" is declared on NUS RX data (see handleRX).
	adapter.SetConnectHandler(func(device bluetooth.Device, connected bool) {
		addr := device.Address.String()
		s.rxMu.Lock()
		disconnect := false
		if connected {
			s.links[addr] = true
			s.lastUp = addr
			log.Printf("[ble] transport connected: %s (links=%d) — awaiting NUS data", addr, len(s.links))
		} else {
			delete(s.links, addr)
			log.Printf("[ble] transport disconnected: %s (links=%d)", addr, len(s.links))
			if s.connected && (addr == s.clientAddr || len(s.links) == 0) {
				// Drop a leftover partial line from the prior session.
				s.connected = false
				s.clientAddr = ""
				s.rxBuf.Reset()
				disconnect = true
			}
		}
		s.rxMu.Unlock()

		if disconnect {
			log.Println("[ble] device disconnected")
			s.evtCh <- false
		}
	})

	// Secure-* flags dropped: Claude Desktop's Mac client doesn't trigger SMP, so encrypted-only characteristics would be unreachable.
	err := adapter.AddService(&bluetooth.Service{
		UUID: nusServiceUUID,
		Characteristics: []bluetooth.CharacteristicConfig{
			{
				UUID: nusRXUUID, // Desktop writes here (Desktop → Device)
				Flags: bluetooth.CharacteristicWritePermission |
					bluetooth.CharacteristicWriteWithoutResponsePermission,
				WriteEvent: func(client bluetooth.Connection, offset int, value []byte) {
					s.handleRX(value)
				},
			},
			{
				Handle: &s.txChar,
				UUID:   nusTXUUID, // Device writes here (Device → Desktop)
				Flags: bluetooth.CharacteristicNotifyPermission |
					bluetooth.CharacteristicReadPermission,
			},
		},
	})
	if err != nil {
		return err
	}

	log.Printf("[ble] advertising as %q...", s.deviceName)
	adv := adapter.DefaultAdvertisement()
	err = adv.Configure(bluetooth.AdvertisementOptions{
		LocalName:    s.deviceName,
		ServiceUUIDs: []bluetooth.UUID{nusServiceUUID},
	})
	if err != nil {
		return err
	}

	log.Println("[ble] calling adv.Start()...")
	if err := adv.Start(); err != nil {
		log.Printf("[ble] adv.Start() failed: %v", err)
	} else {
		log.Println("[ble] adv.Start() succeeded")
	}

	log.Println("[ble] BLE advertising started")
	return nil
}

// handleRX buffers incoming bytes and forwards each complete newline-terminated line to the processor; rxMu guards rxBuf.
func (s *BLEServer) handleRX(data []byte) {
	s.rxMu.Lock()
	justConnected := false
	if !s.connected {
		// NUS RX data is the real Desktop-connected signal; attribute it to the most recent transport connect.
		s.connected = true
		s.clientAddr = s.lastUp
		justConnected = true
	}
	clientAddr := s.clientAddr
	s.rxBuf.Write(data)

	var lines [][]byte
	for {
		buf := s.rxBuf.Bytes()
		idx := bytes.IndexByte(buf, '\n')
		if idx < 0 {
			break
		}
		if idx > 0 {
			line := make([]byte, idx)
			copy(line, buf[:idx])
			lines = append(lines, line)
		}
		s.rxBuf.Next(idx + 1)
	}
	s.rxMu.Unlock()

	if justConnected {
		log.Printf("[ble] device connected (NUS client %s)", clientAddr)
		s.evtCh <- true
	}
	for _, line := range lines {
		s.msgCh <- line
	}
}

// Send writes a JSON line to the TX characteristic in 180-byte chunks (under the ~185 macOS MTU); sendMu serializes writes.
func (s *BLEServer) Send(data []byte) error {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	const maxNotify = 180
	for len(data) > 0 {
		chunk := data
		if len(chunk) > maxNotify {
			chunk = data[:maxNotify]
		}
		if _, err := s.txChar.Write(chunk); err != nil {
			return err
		}
		data = data[len(chunk):]
	}
	return nil
}

// Close is a no-op for tinygo bluetooth.
func (s *BLEServer) Close() {}

// tuneAdvIntervals sets 100-200 ms LE adv intervals (0.625 ms units) via hci debugfs; best-effort.
func tuneAdvIntervals() {
	const minVal = "160" // 100 ms
	const maxVal = "320" // 200 ms

	// Try every hci<n>; the controller isn't always hci0.
	matches, err := filepath.Glob("/sys/kernel/debug/bluetooth/hci*")
	if err != nil || len(matches) == 0 {
		log.Printf("[ble] WARN: bluetooth debugfs not available — using BlueZ default 1280ms advertising")
		return
	}

	for _, dir := range matches {
		minPath := filepath.Join(dir, "adv_min_interval")
		maxPath := filepath.Join(dir, "adv_max_interval")
		// Write min first: we only lower, and the kernel rejects min > current max.
		if err := os.WriteFile(minPath, []byte(minVal), 0644); err != nil {
			log.Printf("[ble] WARN: tune %s: %v", minPath, err)
			continue
		}
		if err := os.WriteFile(maxPath, []byte(maxVal), 0644); err != nil {
			log.Printf("[ble] WARN: tune %s: %v", maxPath, err)
			continue
		}
		log.Printf("[ble] tuned advertising interval on %s: min=%s max=%s (units of 0.625ms)", dir, minVal, maxVal)
	}
}
