package domain

type Network struct {
	BSSID    string `json:"bssid"`
	SSID     string `json:"ssid"`
	Mode     string `json:"mode"`
	Channel  int    `json:"channel"`
	Rate     string `json:"rate"`
	Signal   int    `json:"signal"`
	LinkRate int    `json:"linkRate"` // Mbps; 0 = unknown
	Security string `json:"security"`
}

type ListNetworkResponse struct {
	Networks []Network `json:"networks"`
}

type CurrentNetworkResponse struct {
	BSSID    string `json:"bssid"`
	SSID     string `json:"ssid"`
	Mode     string `json:"mode"`
	Channel  int    `json:"channel"`
	Rate     int    `json:"rate"`
	Signal   int    `json:"signal"`
	Security string `json:"security"`
}

type SetupNetworkRequest struct {
	SSID     string `json:"ssid"`
	Password string `json:"password"`
}

type SetupNetworkResponse struct {
	Success bool `json:"success"`
}
