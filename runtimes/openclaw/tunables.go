package openclaw

import "time"

// ModelsAPIURL is the FULL upstream URL each device fetches to list available LLM models.
const ModelsAPIURL = "https://campaign-api.autonomous.ai/api/v1/ai/v1/models"

// ModelSyncInterval is how often each device re-fetches ModelsAPIURL and reconciles the result into openclaw.json under s.config.OpenclawConfigDir.
const ModelSyncInterval = 30 * time.Minute

// modelsAPITimeout caps a single upstream fetch so the sync loop never blocks forever on a hung connection.
const modelsAPITimeout = 15 * time.Second
