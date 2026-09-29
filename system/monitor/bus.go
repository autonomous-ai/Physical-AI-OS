package monitor

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/domain"
)

const bufferSize = 200

// Bus is a runtime-agnostic event bus for monitor events.
// It manages a ring buffer and fan-out to SSE subscribers.
type Bus struct {
	events []domain.MonitorEvent
	mu     sync.RWMutex
	subs   map[int]chan domain.MonitorEvent
	subID  int
	evtID  atomic.Int64
}

// ProvideBus constructs a monitor event bus.
func ProvideBus() *Bus {
	return &Bus{}
}

// Push adds an event to the ring buffer and notifies all SSE subscribers.
func (b *Bus) Push(evt domain.MonitorEvent) {
	if evt.ID == "" {
		evt.ID = fmt.Sprintf("evt-%d", b.evtID.Add(1))
	}
	if evt.Time == "" {
		evt.Time = time.Now().UTC().Format(time.RFC3339Nano)
	}

	b.mu.Lock()
	b.events = append(b.events, evt)
	if len(b.events) > bufferSize {
		b.events = b.events[len(b.events)-bufferSize:]
	}
	subs := make([]chan domain.MonitorEvent, 0, len(b.subs))
	for _, ch := range b.subs {
		subs = append(subs, ch)
	}
	b.mu.Unlock()

	for _, ch := range subs {
		select {
		case ch <- evt:
		default:
			// subscriber too slow, drop event
		}
	}
}

// Subscribe returns a channel that receives new monitor events and an unsubscribe function.
func (b *Bus) Subscribe() (<-chan domain.MonitorEvent, func()) {
	ch := make(chan domain.MonitorEvent, 32)
	b.mu.Lock()
	if b.subs == nil {
		b.subs = make(map[int]chan domain.MonitorEvent)
	}
	b.subID++
	id := b.subID
	b.subs[id] = ch
	b.mu.Unlock()

	unsub := func() {
		b.mu.Lock()
		delete(b.subs, id)
		b.mu.Unlock()
		// Do not close ch: Push sends after releasing the mutex, so closing races
		// with a send. Once removed from subs it has no producers.
	}
	return ch, unsub
}

// Recent returns the last n events from the ring buffer.
func (b *Bus) Recent(n int) []domain.MonitorEvent {
	b.mu.RLock()
	defer b.mu.RUnlock()
	total := len(b.events)
	if n <= 0 || n > total {
		n = total
	}
	result := make([]domain.MonitorEvent, n)
	copy(result, b.events[total-n:])
	return result
}
