package mqtt

// Factory creates MQTT clients sharing one config; each gets a unique client ID.
type Factory struct {
	config Config
}

// ProvideFactory creates a Factory from config.
func ProvideFactory(cfg Config) (*Factory, error) {
	return &Factory{config: cfg}, nil
}

// UpdateConfig refreshes the connection config; call before restartMQTT to pick up new credentials.
func (f *Factory) UpdateConfig(cfg Config) {
	f.config = cfg
}

// CreateClient returns a new client with a unique ID; call Connect, Publish, then Close.
func (f *Factory) GetClient(clientID string) *MQTT {
	return ProvideClient(Options{
		Endpoint: f.config.Endpoint,
		Port:     f.config.Port,
		Username: f.config.Username,
		Password: f.config.Password,
		ClientID: clientID,
	})
}
