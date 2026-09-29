package mqtthandler

import "testing"

func TestFigmaStdioEntry_OAuthAndPAT(t *testing.T) {
	// OAuth: token in access_token, default descriptor -> Authorization/Bearer.
	oauth := figmaStdioEntry("/tmp/ocdir/server.mjs", ConnectorCreds{AuthType: "oauth", AccessToken: "oauthtok"})
	env, _ := oauth["env"].(map[string]any)
	if env["FIGMA_TOKEN"] != "oauthtok" {
		t.Fatalf("oauth FIGMA_TOKEN = %v, want oauthtok", env["FIGMA_TOKEN"])
	}
	if env["FIGMA_AUTH_HEADER"] != "Authorization" {
		t.Fatalf("oauth FIGMA_AUTH_HEADER = %v, want Authorization", env["FIGMA_AUTH_HEADER"])
	}
	if env["FIGMA_ACCESS_TOKEN"] != "oauthtok" {
		t.Fatalf("oauth FIGMA_ACCESS_TOKEN alias = %v, want oauthtok", env["FIGMA_ACCESS_TOKEN"])
	}

	pat := figmaStdioEntry("/tmp/ocdir/server.mjs", ConnectorCreds{
		AuthType: "pat", APIKey: "pat123",
		Credentials: map[string]string{"mcp_auth_header": "header:X-Figma-Token"},
	})
	penv, _ := pat["env"].(map[string]any)
	if penv["FIGMA_TOKEN"] != "pat123" {
		t.Fatalf("pat FIGMA_TOKEN = %v, want pat123", penv["FIGMA_TOKEN"])
	}
	if penv["FIGMA_AUTH_HEADER"] != "X-Figma-Token" {
		t.Fatalf("pat FIGMA_AUTH_HEADER = %v, want X-Figma-Token", penv["FIGMA_AUTH_HEADER"])
	}
}
