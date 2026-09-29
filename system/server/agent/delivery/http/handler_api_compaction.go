package http

import (
	"bufio"
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

const (
	openclawSessionsIndex = "/root/.openclaw/agents/main/sessions/sessions.json"
	defaultMainSessionKey = "agent:main:main"

	// Records embed the full summary in one JSONL line (~16000 chars observed); 4 MiB headroom.
	compactionLineBufMax = 4 * 1024 * 1024
)

type compactionRecord struct {
	Type             string         `json:"type"`
	ID               any            `json:"id"`
	ParentID         any            `json:"parentId"`
	Timestamp        string         `json:"timestamp"`
	Summary          string         `json:"summary"`
	TokensBefore     int            `json:"tokensBefore"`
	Details          map[string]any `json:"details"`
	FromHook         bool           `json:"fromHook"`
	FirstKeptEntryID any            `json:"firstKeptEntryId"`
}

// CompactionLatest returns the compaction summary active at a given time for an OpenClaw session.
// Query: session=<key> (default agent:main:main), at=<iso-ts> (default: newest record).
func (h *AgentHandler) CompactionLatest(c *gin.Context) {
	raw, err := os.ReadFile(openclawSessionsIndex)
	if err != nil {
		c.JSON(http.StatusNotFound, serializers.ResponseError("sessions index not found: "+err.Error()))
		return
	}
	var sessions map[string]map[string]any
	if err := json.Unmarshal(raw, &sessions); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("parse sessions.json: "+err.Error()))
		return
	}
	sessionKey := c.DefaultQuery("session", defaultMainSessionKey)
	entry, ok := sessions[sessionKey]
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("session key not found: "+sessionKey))
		return
	}
	sessionFile, _ := entry["sessionFile"].(string)
	if sessionFile == "" {
		sid, _ := entry["sessionId"].(string)
		if sid == "" {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("session has no sessionFile or sessionId"))
			return
		}
		sessionFile = filepath.Join(filepath.Dir(openclawSessionsIndex), sid+".jsonl")
	}

	atCutoff := strings.TrimSpace(c.Query("at"))
	latest, nextTs, err := scanActiveCompaction(sessionFile, atCutoff)
	if err != nil {
		c.JSON(http.StatusNotFound, serializers.ResponseError("session file scan failed: "+err.Error()))
		return
	}
	if latest == nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"found":       false,
			"sessionKey":  sessionKey,
			"sessionFile": sessionFile,
			"atQuery":     atCutoff,
		}))
		return
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"found":            true,
		"sessionKey":       sessionKey,
		"sessionFile":      sessionFile,
		"compactionCount":  entry["compactionCount"],
		"id":               latest.ID,
		"parentId":         latest.ParentID,
		"timestamp":        latest.Timestamp,
		"nextTimestamp":    nextTs, // "" if this is still the active compaction
		"tokensBefore":     latest.TokensBefore,
		"summaryChars":     len(latest.Summary),
		"summary":          latest.Summary,
		"details":          latest.Details,
		"fromHook":         latest.FromHook,
		"firstKeptEntryId": latest.FirstKeptEntryID,
		"atQuery":          atCutoff,
	}))
}

// scanActiveCompaction returns the record active at atCutoff (empty = newest) and the next
// record's timestamp ("" if still active).
func scanActiveCompaction(path, atCutoff string) (*compactionRecord, string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, "", err
	}
	defer f.Close()

	needle := []byte(`"type":"compaction"`)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), compactionLineBufMax)

	var active *compactionRecord
	var nextTs string
	for scanner.Scan() {
		line := scanner.Bytes()
		if !bytes.Contains(line, needle) {
			continue
		}
		var rec compactionRecord
		if err := json.Unmarshal(line, &rec); err != nil {
			continue
		}
		if rec.Type != "compaction" {
			continue
		}
		if atCutoff != "" && rec.Timestamp > atCutoff {
			// Past the cutoff: this is the successor that ends the active record's window.
			if active != nil {
				nextTs = rec.Timestamp
				break
			}
			continue
		}
		cp := rec
		active = &cp
	}
	if err := scanner.Err(); err != nil {
		return nil, "", err
	}
	return active, nextTs, nil
}
