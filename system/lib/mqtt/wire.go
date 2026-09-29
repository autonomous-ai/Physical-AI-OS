package mqtt

import "github.com/google/wire"

// ProviderSet exposes the MQTT factory and client providers for Wire.
var ProviderSet = wire.NewSet(
	ProvideFactory,
	ProvideClient,
)
