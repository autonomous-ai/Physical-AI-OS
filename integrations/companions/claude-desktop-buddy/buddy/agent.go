package buddy

import (
	"fmt"
	"log"

	"github.com/godbus/dbus/v5"
	"github.com/godbus/dbus/v5/introspect"
)

// agentPath is the D-Bus object path BlueZ calls back into; must stay stable for the process lifetime.
const agentPath = "/ai/autonomous/buddy/agent"

// Agent implements org.bluez.Agent1 as DisplayOnly; the passkey is logged for the operator to type into Claude Desktop.
type Agent struct{}

// Release is called when BlueZ unregisters the agent (e.g. on shutdown).
func (a *Agent) Release() *dbus.Error {
	log.Println("[agent] released")
	return nil
}

// Cancel is called when BlueZ cancels an in-progress pairing.
func (a *Agent) Cancel() *dbus.Error {
	log.Println("[agent] cancel pairing")
	return nil
}

// AuthorizeService accepts every service request; access control is left to GATT security flags.
func (a *Agent) AuthorizeService(device dbus.ObjectPath, uuid string) *dbus.Error {
	log.Printf("[agent] authorize service %s on %s", uuid, device)
	return nil
}

// DisplayPasskey logs the pairing passkey the user must enter on the desktop.
func (a *Agent) DisplayPasskey(device dbus.ObjectPath, passkey uint32, entered uint16) *dbus.Error {
	log.Printf("[agent] PAIRING PASSKEY for %s: %06d (entered %d/6)", device, passkey, entered)
	return nil
}

// DisplayPinCode is the legacy BR/EDR equivalent of DisplayPasskey.
func (a *Agent) DisplayPinCode(device dbus.ObjectPath, pincode string) *dbus.Error {
	log.Printf("[agent] PAIRING PIN for %s: %s", device, pincode)
	return nil
}

// RequestPasskey should never fire for DisplayOnly; returns 0 to signal failure.
func (a *Agent) RequestPasskey(device dbus.ObjectPath) (uint32, *dbus.Error) {
	log.Printf("[agent] WARN: RequestPasskey called on DisplayOnly agent for %s", device)
	return 0, dbus.NewError("org.bluez.Error.Rejected", nil)
}

// RequestPinCode is the legacy BR/EDR equivalent. Not used in LE flows.
func (a *Agent) RequestPinCode(device dbus.ObjectPath) (string, *dbus.Error) {
	log.Printf("[agent] WARN: RequestPinCode called on DisplayOnly agent for %s", device)
	return "", dbus.NewError("org.bluez.Error.Rejected", nil)
}

// RequestConfirmation auto-accepts Just Works / Numeric Comparison so headless setups work.
func (a *Agent) RequestConfirmation(device dbus.ObjectPath, passkey uint32) *dbus.Error {
	log.Printf("[agent] confirm pairing for %s passkey=%06d (auto-accept)", device, passkey)
	return nil
}

// RequestAuthorization auto-accepts bonding without numeric comparison.
func (a *Agent) RequestAuthorization(device dbus.ObjectPath) *dbus.Error {
	log.Printf("[agent] authorization for %s (auto-accept)", device)
	return nil
}

// agentIntrospectXML answers BlueZ's introspection probe; some BlueZ versions reject the agent without it.
const agentIntrospectXML = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
    <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
    <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
    <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
</node>`

// registerBluezAgent exports Agent on the system D-Bus and registers it as BlueZ's default DisplayOnly agent.
func registerBluezAgent() error {
	conn, err := dbus.SystemBus()
	if err != nil {
		return fmt.Errorf("connect system bus: %w", err)
	}

	agent := &Agent{}
	if err := conn.Export(agent, agentPath, "org.bluez.Agent1"); err != nil {
		return fmt.Errorf("export agent: %w", err)
	}
	if err := conn.Export(introspect.Introspectable(agentIntrospectXML), agentPath,
		"org.freedesktop.DBus.Introspectable"); err != nil {
		return fmt.Errorf("export introspect: %w", err)
	}

	mgr := conn.Object("org.bluez", "/org/bluez")

	// Best-effort: drop a stale registration from a prior run.
	mgr.Call("org.bluez.AgentManager1.UnregisterAgent", 0, dbus.ObjectPath(agentPath))

	if call := mgr.Call("org.bluez.AgentManager1.RegisterAgent", 0,
		dbus.ObjectPath(agentPath), "DisplayOnly"); call.Err != nil {
		return fmt.Errorf("register agent: %w", call.Err)
	}
	if call := mgr.Call("org.bluez.AgentManager1.RequestDefaultAgent", 0,
		dbus.ObjectPath(agentPath)); call.Err != nil {
		return fmt.Errorf("request default agent: %w", call.Err)
	}

	log.Println("[agent] registered with BlueZ as DisplayOnly default agent")
	return nil
}
