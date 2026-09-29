// Package osreset holds the path-wipe primitive shared by factory reset and agent backends.
package osreset

import (
	"log"
	"os"
)

// WipePath recursively removes path, logging under prefix; missing paths and failures are non-fatal.
func WipePath(prefix, p string) {
	if _, err := os.Stat(p); os.IsNotExist(err) {
		return
	}
	if err := os.RemoveAll(p); err != nil {
		if os.IsNotExist(err) {
			return
		}
		log.Printf("%s wipe %s: %v (non-fatal)", prefix, p, err)
		return
	}
	log.Printf("%s wiped %s", prefix, p)
}
