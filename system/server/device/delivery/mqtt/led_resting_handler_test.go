package mqtthandler

import (
	"encoding/json"
	"net/http"
	"reflect"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

const restingReply = `{"mode":"custom","color":[8,4,1],"default":{"effect":"solid","color":[5,4,3]},"effective":{"effect":"solid","color":[8,4,1]}}`

func halCall(t *testing.T, stub *voiceHAL) voiceHALCall {
	t.Helper()
	select {
	case c := <-stub.calls:
		return c
	case <-time.After(3 * time.Second):
		t.Fatal("missing HAL call")
	}
	return voiceHALCall{}
}

func TestLEDRestingGetRelaysHALSnapshot(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/led/resting", http.StatusOK, restingReply)

	send(domain.KindLEDRestingGet, `{}`)
	var got, want any
	if err := json.Unmarshal([]byte(replyData(t, read(t, domain.KindLEDRestingGet, "success"))), &got); err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal([]byte(restingReply), &want)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("data = %v", got)
	}
	if c := halCall(t, stub); c.path != "/led/resting" || c.body != nil {
		t.Fatalf("HAL call = %+v", c)
	}
}

func TestLEDRestingSetSavesCustomColour(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/led/resting", http.StatusOK, restingReply)

	send(domain.KindLEDRestingSet, `{"mode":"custom","color":[8,4,1]}`)
	read(t, domain.KindLEDRestingSet, "success")
	c := halCall(t, stub)
	want := map[string]any{"mode": "custom", "color": []any{8.0, 4.0, 1.0}}
	if c.path != "/led/resting" || !reflect.DeepEqual(c.body, want) {
		t.Fatalf("HAL call = %+v", c)
	}
}

func TestLEDRestingSetOffSendsNoColour(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/led/resting", http.StatusOK, `{"mode":"off","color":null,"default":{"effect":"solid","color":[5,4,3]},"effective":{"effect":"solid","color":[0,0,0]}}`)

	send(domain.KindLEDRestingSet, `{"mode":"off","color":[1,2,3]}`)
	read(t, domain.KindLEDRestingSet, "success")
	if c := halCall(t, stub); !reflect.DeepEqual(c.body, map[string]any{"mode": "off"}) {
		t.Fatalf("HAL body = %v", c.body)
	}
}

func TestLEDRestingSetRejectsBadInputWithoutCallingHAL(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	for _, data := range []string{
		`{"mode":"rainbow"}`,
		`{"mode":"custom"}`,
		`{"mode":"custom","color":[1,2]}`,
		`{"mode":"custom","color":[1,2,256]}`,
		`not json`,
	} {
		send(domain.KindLEDRestingSet, data)
		read(t, domain.KindLEDRestingSet, "failure")
	}
	stub.noCall(t)
}

func TestLEDRestingSetReportsHALError(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/led/resting", http.StatusServiceUnavailable, `{"detail":"LED not available"}`)

	send(domain.KindLEDRestingSet, `{"mode":"default"}`)
	if r := read(t, domain.KindLEDRestingSet, "failure"); r.Error != "PUT /led/resting returned 503: LED not available" {
		t.Fatalf("error = %q", r.Error)
	}
}

func TestLEDRestingPreviewIsSilentOnSuccess(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/led/resting/preview", http.StatusOK, `{"status":"ok","painted":true}`)

	send(domain.KindLEDRestingPreview, `{"color":[20,0,10]}`)
	c := halCall(t, stub)
	if c.path != "/led/resting/preview" || !reflect.DeepEqual(c.body, map[string]any{"color": []any{20.0, 0.0, 10.0}}) {
		t.Fatalf("HAL call = %+v", c)
	}

	// A bad colour is the only reply the app sees.
	send(domain.KindLEDRestingPreview, `{"color":[20,0]}`)
	read(t, domain.KindLEDRestingPreview, "failure")
}
