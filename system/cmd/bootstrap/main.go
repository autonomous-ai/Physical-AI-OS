package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"log/slog"

	"github.com/joho/godotenv"

	"go.autonomous.ai/os/system/bootstrap"
	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/lib/logger"
	"go.autonomous.ai/os/system/lib/syspath"
)

func main() {
	var showVersion bool
	flag.BoolVar(&showVersion, "version", false, "print version and exit")
	flag.Parse()

	if showVersion {
		fmt.Println(config.BootstrapVersion)
		return
	}

	// Load before logger init so GELF_* vars are visible; missing file is fine.
	_ = godotenv.Load("/opt/hal/.env")

	cleanup := logger.Init("/var/log/bootstrap.log")
	defer cleanup()
	// Filed as bootstrap, not os-server, and spooled until a key exists: OTA
	// runs during and right after setup, so its records explain setup failures.
	logger.SetGELFServiceName("bootstrap")
	logger.EnableGELFSpool(syspath.GELFSpoolDir(), "bootstrap")
	go bootstrap.RunLogRelay(context.Background())

	b, err := bootstrap.ProvideServer()
	if err != nil {
		log.Fatalf("bootstrap: initialize: %v", err)
	}
	if err := b.Serve(); err != nil {
		log.Fatalf("bootstrap: %v", err)
	}
	slog.Info("bootstrap stopped", "component", "bootstrap")
}
