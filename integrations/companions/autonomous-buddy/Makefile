# Autonomous Buddy — macOS build/run shortcuts.
# Run from this directory: `make <target>`. `make help` for the list.

APP_NAME   := AutonomousBuddy
BUNDLE_ID  := network.autonomous.ai.buddy
VERSION    := $(shell cat VERSION_AUTONOMOUS_BUDDY | tr -d '[:space:]')

MACOS_DIR  := macos
DIST_DIR   := dist
INFO_PLIST := $(MACOS_DIR)/Info.plist
BUILD_DIR  := $(MACOS_DIR)/.build
# Universal binary output path — `swift build --arch arm64 --arch x86_64` writes here,
# not the per-arch `.build/release/`. Lets one DMG run on both Apple Silicon and Intel.
RELEASE_BIN := $(BUILD_DIR)/apple/Products/Release/$(APP_NAME)
APP_BUNDLE := $(DIST_DIR)/$(APP_NAME).app
DMG_FILE   := $(DIST_DIR)/$(APP_NAME)-$(VERSION).dmg
DMG_STAGE  := $(DIST_DIR)/dmg-stage
DMG_VOL    := $(APP_NAME) $(VERSION)
ICON_SCRIPT := $(MACOS_DIR)/scripts/gen-app-icon.swift
ICON_MASTER := $(DIST_DIR)/AppIcon-master.png
ICONSET_DIR := $(DIST_DIR)/AppIcon.iconset
ICNS_FILE  := $(DIST_DIR)/AppIcon.icns
AUDIT_LOG  := $(HOME)/Library/Application Support/AutonomousBuddy/audit.log

MOCK_DIR := mock-device

.PHONY: help native-run native-build native-release native-app native-open native-clean audit native-kill native-all native-check mock mock-build native-dmg icon native-app-signed native-dmg-signed native-notarize

.DEFAULT_GOAL := help

help: ## Show this help
	@echo "Autonomous Buddy — make targets:"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Typical flow:"
	@echo "  make build && make install  # one app with agent manager + native helper"
	@echo "  make open"
	@echo ""
	@echo "Signing is automatic when a Developer ID cert is in the keychain."

native-run: ## Run buddy in dev mode (foreground)
	cd $(MACOS_DIR) && swift run

native-build: ## Build debug binary
	cd $(MACOS_DIR) && swift build

native-release: ## Build universal release binary (arm64 + x86_64) at macos/.build/apple/Products/Release/AutonomousBuddy
	cd $(MACOS_DIR) && swift build -c release --arch arm64 --arch x86_64

native-check: ## Compile-only check (no link)
	cd $(MACOS_DIR) && swift build --build-tests

native-app: native-release $(ICNS_FILE) ## Bundle into dist/AutonomousBuddy.app (Developer ID signed if a cert is installed, else ad-hoc)
	@echo "==> Bundling $(APP_BUNDLE)"
	@rm -rf "$(APP_BUNDLE)"
	@mkdir -p "$(APP_BUNDLE)/Contents/MacOS"
	@mkdir -p "$(APP_BUNDLE)/Contents/Resources"
	@cp "$(RELEASE_BIN)" "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@cp "$(INFO_PLIST)" "$(APP_BUNDLE)/Contents/Info.plist"
	@cp "$(ICNS_FILE)" "$(APP_BUNDLE)/Contents/Resources/AppIcon.icns"
	@chmod +x "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@echo "==> Signing with $(SIGN_ID)"
	@codesign --force $(SIGN_FLAGS) --sign "$(SIGN_ID)" --identifier "$(BUNDLE_ID)" \
		"$(APP_BUNDLE)" 2>&1 | sed 's/^/    /'
	@codesign --verify --deep --strict "$(APP_BUNDLE)" 2>&1 | sed 's/^/    /'
	@echo "==> Done: $(APP_BUNDLE)"
	@if [ -z "$(DEV_ID_APP)" ]; then \
		echo "    Ad-hoc signed — first launch: right-click → Open."; \
	else \
		echo "    Developer ID signed. Not notarized: run 'make native-dmg-signed' for the ship-ready DMG."; \
	fi

icon: $(ICNS_FILE) ## Regenerate AppIcon.icns from the SF Symbol placeholder

# Swift script → 1024 master PNG → sips iconset sizes → iconutil .icns (regenerated only when missing).
$(ICNS_FILE): $(ICON_SCRIPT)
	@echo "==> Generating $(ICNS_FILE) from SF Symbol placeholder"
	@mkdir -p "$(DIST_DIR)"
	@swift "$(ICON_SCRIPT)" "$(ICON_MASTER)"
	@rm -rf "$(ICONSET_DIR)"
	@mkdir -p "$(ICONSET_DIR)"
	@sips -z 16   16   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_16x16.png"      >/dev/null
	@sips -z 32   32   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_16x16@2x.png"   >/dev/null
	@sips -z 32   32   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_32x32.png"      >/dev/null
	@sips -z 64   64   "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_32x32@2x.png"   >/dev/null
	@sips -z 128  128  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_128x128.png"    >/dev/null
	@sips -z 256  256  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_128x128@2x.png" >/dev/null
	@sips -z 256  256  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_256x256.png"    >/dev/null
	@sips -z 512  512  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_256x256@2x.png" >/dev/null
	@sips -z 512  512  "$(ICON_MASTER)" --out "$(ICONSET_DIR)/icon_512x512.png"    >/dev/null
	@cp "$(ICON_MASTER)" "$(ICONSET_DIR)/icon_512x512@2x.png"
	@iconutil -c icns "$(ICONSET_DIR)" -o "$(ICNS_FILE)"
	@rm -rf "$(ICONSET_DIR)"
	@echo "==> Done: $(ICNS_FILE)"

native-dmg: native-app ## Package the .app into a distributable DMG (drag-to-Applications layout)
	@echo "==> Packaging $(DMG_FILE)"
	@rm -f "$(DMG_FILE)"
	@rm -rf "$(DMG_STAGE)"
	@mkdir -p "$(DMG_STAGE)"
	@cp -R "$(APP_BUNDLE)" "$(DMG_STAGE)/"
	@ln -s /Applications "$(DMG_STAGE)/Applications"
	@hdiutil create -volname "$(DMG_VOL)" -srcfolder "$(DMG_STAGE)" \
		-ov -format UDZO "$(DMG_FILE)" >/dev/null
	@rm -rf "$(DMG_STAGE)"
	@if [ -n "$(DEV_ID_APP)" ]; then \
		echo "==> Signing DMG with $(DEV_ID_APP)"; \
		codesign --force --sign "$(DEV_ID_APP)" --timestamp "$(DMG_FILE)"; \
	fi
	@echo "==> Done: $(DMG_FILE)"
	@echo "    Send this DMG to users. They mount it and drag $(APP_NAME).app onto Applications."
	@if [ -z "$(DEV_ID_APP)" ]; then \
		echo "    Ad-hoc: first launch needs right-click → Open."; \
	else \
		echo "    Developer ID signed but NOT notarized — first launch still needs right-click → Open."; \
		echo "    Run 'make native-dmg-signed' (needs NOTARY_PROFILE) for a DMG that opens with a double-click."; \
	fi

native-open: ## Launch the bundled .app from dist/
	@if [ ! -d "$(APP_BUNDLE)" ]; then echo "No bundle yet — run 'make native-app' first."; exit 1; fi
	open "$(APP_BUNDLE)"

native-kill: ## Stop any running AutonomousBuddy process
	-@pkill -x $(APP_NAME) 2>/dev/null && echo "Stopped $(APP_NAME)" || echo "$(APP_NAME) not running"

audit: ## Tail the audit log
	@if [ ! -f "$(AUDIT_LOG)" ]; then echo "No audit log yet at $(AUDIT_LOG)"; exit 0; fi
	@tail -f "$(AUDIT_LOG)"

native-clean: ## Remove all build artifacts
	@echo "==> Cleaning"
	cd $(MACOS_DIR) && swift package clean 2>/dev/null || true
	rm -rf $(BUILD_DIR) $(MACOS_DIR)/.swiftpm $(DIST_DIR)

native-all: native-app ## Alias for `make native-app`

mock: ## Run the mock device server (Go) — test buddy without a real device
	cd $(MOCK_DIR) && go run .

mock-build: ## Compile-only check for mock-device
	cd $(MOCK_DIR) && go vet ./... && go build ./...

# Release signing (Developer ID). Env: DEV_ID_APP (identity from `security find-identity -v`),
# NOTARY_PROFILE (from `xcrun notarytool store-credentials`). See docs/release-signing.md.

# Auto-detected from the keychain so `make native-app` / `install` / `dmg` sign for
# distribution without extra env. Override by exporting DEV_ID_APP, or force a
# local ad-hoc build with `make native-app DEV_ID_APP=`.
DEV_ID_APP     ?= $(shell security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)
NOTARY_PROFILE ?=

# Ad-hoc ("-") when no Developer ID is available; hardened runtime + secure
# timestamp only make sense with a real identity.
SIGN_ID    = $(if $(DEV_ID_APP),$(DEV_ID_APP),-)
SIGN_FLAGS = $(if $(DEV_ID_APP),--options runtime --timestamp,)

native-app-signed: ## Same as `make native-app`, but fails if no Developer ID identity is available
	@if [ -z "$(DEV_ID_APP)" ]; then \
		echo "ERROR: no Developer ID Application identity found and DEV_ID_APP is unset."; \
		echo "       See autonomous-buddy/docs/release-signing.md for setup."; \
		exit 1; \
	fi
	@$(MAKE) native-app
	@spctl --assess --type execute --verbose=4 "$(APP_BUNDLE)" 2>&1 | sed 's/^/    /' || true

native-dmg-signed: ## Build signed .app → package DMG → sign DMG → notarize → staple (requires DEV_ID_APP + NOTARY_PROFILE)
	@if [ -z "$(NOTARY_PROFILE)" ]; then \
		echo "ERROR: NOTARY_PROFILE env var is required (keychain profile created via xcrun notarytool store-credentials)."; \
		echo "       See autonomous-buddy/docs/release-signing.md for setup."; \
		exit 1; \
	fi
	@$(MAKE) native-app-signed
	@$(MAKE) native-dmg
	@echo "==> Submitting to Apple notarization (this can take 1-5 min)"
	@xcrun notarytool submit "$(DMG_FILE)" \
		--keychain-profile "$(NOTARY_PROFILE)" \
		--wait
	@echo "==> Stapling ticket"
	@xcrun stapler staple "$(DMG_FILE)"
	@xcrun stapler validate "$(DMG_FILE)" 2>&1 | sed 's/^/    /'
	@spctl --assess --type open --context context:primary-signature --verbose=4 "$(DMG_FILE)" 2>&1 | sed 's/^/    /' || true
	@echo "==> Done: $(DMG_FILE) (signed + notarized + stapled)"
	@echo "    Send this DMG to users — Gatekeeper accepts without right-click → Open."

native-notarize: ## Re-submit an existing DMG for notarization (skips build + sign; use if a previous notarize failed)
	@if [ -z "$(NOTARY_PROFILE)" ]; then echo "ERROR: NOTARY_PROFILE required"; exit 1; fi
	@if [ ! -f "$(DMG_FILE)" ]; then echo "ERROR: $(DMG_FILE) not found — run 'make native-dmg-signed' first"; exit 1; fi
	@xcrun notarytool submit "$(DMG_FILE)" --keychain-profile "$(NOTARY_PROFILE)" --wait
	@xcrun stapler staple "$(DMG_FILE)"
	@xcrun stapler validate "$(DMG_FILE)"

# Unified product: Electron owns the visible app and embeds the Swift helper.
DESKTOP_DIR := desktop
BUDDY_ARCH ?= $(shell node -p process.arch)
BUDDY_ARCHS ?= arm64 x64
UNIFIED_APP := $(DESKTOP_DIR)/artifacts/package/Autonomous Buddy-darwin-$(BUDDY_ARCH)/Autonomous Buddy.app
UNIFIED_DMG := $(DIST_DIR)/Autonomous-Buddy-$(VERSION)-$(BUDDY_ARCH).dmg
UNIFIED_ZIP := $(DIST_DIR)/Autonomous-Buddy-$(VERSION)-$(BUDDY_ARCH).zip
UNIFIED_DMG_STAGE := $(DIST_DIR)/dmg-stage-$(BUDDY_ARCH)
.PHONY: build install app open run check all dmg dmg-arch app-signed dmg-signed notarize notarize-arch update-zip

build: ## Build Electron + Swift and package one Autonomous Buddy.app (current Mac architecture)
	$(MAKE) -C $(DESKTOP_DIR) build DEV_ID_APP="$(DEV_ID_APP)" BUDDY_ARCH="$(BUDDY_ARCH)"

app: build ## Alias for the unified build

install: ## Build and install one application, preserving data and archiving the legacy app
	$(MAKE) -C $(DESKTOP_DIR) install DEV_ID_APP="$(DEV_ID_APP)"

open: ## Open the installed unified Autonomous Buddy
	$(MAKE) -C $(DESKTOP_DIR) open

run: build ## Build and open the unified local bundle
	open "$(UNIFIED_APP)"

check: ## Validate Swift and desktop components
	$(MAKE) native-check
	$(MAKE) -C $(DESKTOP_DIR) check

all: build ## Alias for the unified build

dmg: ## Build separate Apple Silicon and Intel DMGs (override BUDDY_ARCHS to select)
	@set -e; for arch in $(BUDDY_ARCHS); do \
		$(MAKE) dmg-arch BUDDY_ARCH="$$arch"; \
	done

dmg-arch: build ## Package one BUDDY_ARCH application into a DMG (no notarization)
	@mkdir -p "$(DIST_DIR)"
	@rm -rf "$(UNIFIED_DMG_STAGE)"
	@mkdir -p "$(UNIFIED_DMG_STAGE)"
	@ditto "$(UNIFIED_APP)" "$(UNIFIED_DMG_STAGE)/Autonomous Buddy.app"
	@ln -s /Applications "$(UNIFIED_DMG_STAGE)/Applications"
	@hdiutil create -volname "Autonomous Buddy $(VERSION) $(BUDDY_ARCH)" -srcfolder "$(UNIFIED_DMG_STAGE)" -ov -format UDZO "$(UNIFIED_DMG)"
	@rm -rf "$(UNIFIED_DMG_STAGE)"
	@if [ -n "$(DEV_ID_APP)" ]; then codesign --force --sign "$(DEV_ID_APP)" --timestamp "$(UNIFIED_DMG)"; fi

app-signed: ## Build the unified app with an explicit Developer ID requirement
	@if [ -z "$(DEV_ID_APP)" ]; then echo "ERROR: DEV_ID_APP is required"; exit 1; fi
	$(MAKE) build DEV_ID_APP="$(DEV_ID_APP)"

dmg-signed: ## Explicit distribution workflow: signed unified DMG, notarization, stapling
	@if [ -z "$(DEV_ID_APP)" ] || [ -z "$(NOTARY_PROFILE)" ]; then echo "ERROR: DEV_ID_APP and NOTARY_PROFILE required"; exit 1; fi
	$(MAKE) dmg DEV_ID_APP="$(DEV_ID_APP)"
	$(MAKE) notarize

notarize: ## Explicitly notarize both existing architecture DMGs
	@set -e; for arch in $(BUDDY_ARCHS); do \
		$(MAKE) notarize-arch BUDDY_ARCH="$$arch"; \
	done

notarize-arch: ## Explicitly notarize one existing BUDDY_ARCH DMG
	@if [ -z "$(NOTARY_PROFILE)" ]; then echo "ERROR: NOTARY_PROFILE required"; exit 1; fi
	@test -f "$(UNIFIED_DMG)"
	xcrun notarytool submit "$(UNIFIED_DMG)" --keychain-profile "$(NOTARY_PROFILE)" --wait
	xcrun stapler staple "$(UNIFIED_DMG)"
	xcrun stapler validate "$(UNIFIED_DMG)"
	spctl --assess --type open --context context:primary-signature --verbose=4 "$(UNIFIED_DMG)"

update-zip: ## Create and verify an updater ZIP from an existing notarized BUDDY_ARCH DMG (no rebuild)
	xcrun stapler validate "$(UNIFIED_DMG)"
	spctl --assess --type open --context context:primary-signature "$(UNIFIED_DMG)"
	node "$(DESKTOP_DIR)/scripts/package-update.mjs" "$(UNIFIED_DMG)" "$(UNIFIED_ZIP)" "$(VERSION)" "$(BUDDY_ARCH)"
