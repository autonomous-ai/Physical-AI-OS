package domain

// SkillDraft is a user-authored skill; it maps onto SKILL.md front-matter plus body.
type SkillDraft struct {
	Name         string `json:"name" binding:"required"`
	Description  string `json:"description" binding:"required"`
	Instructions string `json:"instructions" binding:"required"`
}

// SkillNode is one entry in an installed skill's file tree; Children is set only on directories.
type SkillNode struct {
	Name string `json:"name"`
	// Path is relative to the skills root, e.g. "music/reference/tempo.md".
	Path     string      `json:"path"`
	Dir      bool        `json:"dir,omitempty"`
	Size     int64       `json:"size,omitempty"`
	Children []SkillNode `json:"children,omitempty"`
}

// InstalledSkill is one skill in the active runtime's skills dir; Name is the directory name.
type InstalledSkill struct {
	Name        string      `json:"name"`
	Description string      `json:"description,omitempty"`
	Files       []SkillNode `json:"files"`
	// StoreAvailability is "in_store", "device_only", or "unknown" (catalog unreadable).
	StoreAvailability string `json:"store_availability,omitempty"`
	// UpdatedAt is the newest mtime in the skill's tree (Unix seconds); 0 = unknown.
	UpdatedAt int64 `json:"updated_at,omitempty"`
}

// SkillSummary is an installed skill flattened for the periodic status uplinks (MQTT info, backend ping).
// It omits the file tree on purpose to keep periodic payloads small.
type SkillSummary struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

// SummarizeSkills flattens a ListSkills result; returns nil for an empty list so `skills` is omitted.
func SummarizeSkills(list []InstalledSkill) []SkillSummary {
	if len(list) == 0 {
		return nil
	}
	out := make([]SkillSummary, 0, len(list))
	for _, s := range list {
		out = append(out, SkillSummary{Name: s.Name, Description: s.Description})
	}
	return out
}

// StoreSkillChangelog is one released version's entry in a skill's changelog.
type StoreSkillChangelog struct {
	Version string   `json:"version"`
	Date    string   `json:"date"`
	Changes []string `json:"changes"`
}

// StoreSkill mirrors the catalog's `Skill` schema (upstream snake_case JSON).
type StoreSkill struct {
	ID            string                `json:"id"`
	Name          string                `json:"name"`
	Slug          string                `json:"slug"`
	Description   string                `json:"description"`
	Version       string                `json:"version"`
	CategoryID    string                `json:"category_id"`
	PlanRequired  string                `json:"plan_required"`
	Author        string                `json:"author"`
	License       string                `json:"license"`
	Size          string                `json:"size"`
	IconURL       string                `json:"icon_url"`
	FileURL       string                `json:"file_url"`
	Compatibility []string              `json:"compatibility"`
	Changelog     []StoreSkillChangelog `json:"changelog"`
	DownloadCount int64                 `json:"download_count"`
	Status        int32                 `json:"status"`
	CreatorType   string                `json:"creator_type"`
	Source        string                `json:"source"`
	EnableCount   int64                 `json:"enable_count"`
}

// StoreSkillList is the `data` payload of GET /api/v1/agent-skills.
type StoreSkillList struct {
	Data  []StoreSkill `json:"data"`
	Total int64        `json:"total"`
}

// SkillBundleFile is one file from a `.skill` archive; binary or oversized files carry metadata only.
type SkillBundleFile struct {
	// Path is the entry path inside the archive, e.g. "my-skill/SKILL.md".
	Path string `json:"path"`
	Size int64  `json:"size"`
	Text string `json:"text,omitempty"`
	// Binary marks a file whose bytes aren't valid UTF-8 text.
	Binary bool `json:"binary,omitempty"`
	// Truncated marks a text file cut off at the inline cap.
	Truncated bool `json:"truncated,omitempty"`
}

// SkillBundle is the unpacked contents of a skill archive.
type SkillBundle struct {
	ID    string            `json:"id"`
	Files []SkillBundleFile `json:"files"`
	// Skipped counts entries dropped by the file-count cap.
	Skipped int `json:"skipped,omitempty"`
}
