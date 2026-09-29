// Command claude-desktop-buddy is the buddy daemon entrypoint.
package main

import (
	"flag"
	"io"
	"log"
	"os"

	"claude-desktop-buddy/buddy"

	"gopkg.in/natefinch/lumberjack.v2"
)

func main() {
	configPath := flag.String("config", "/root/config/buddy.json", "path to config file")
	logPath := flag.String("log", "/var/log/claude-desktop-buddy.log", "path to log file")
	flag.Parse()

	// Rotating log file: 2 MB per file, keep 10 backups (same as the OS server).
	rotatingWriter := &lumberjack.Logger{
		Filename:   *logPath,
		MaxSize:    2, // MB
		MaxBackups: 10,
		MaxAge:     0,
		Compress:   false,
	}
	defer rotatingWriter.Close()
	log.SetOutput(io.MultiWriter(os.Stdout, rotatingWriter))
	log.SetFlags(log.Ldate | log.Ltime)

	buddy.Run(*configPath)
}
