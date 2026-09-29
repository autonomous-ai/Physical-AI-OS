import argparse
import time
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from hal.drivers.motors import MotorsService
from hal.drivers.base import Priority

def test_motors_service():
    # Hardware bring-up script: skip unless --id/--port were passed.
    if not {"--id", "--port"}.issubset(set(sys.argv)):
        import pytest

        pytest.skip("needs --id/--port and a real lamp on a serial port")

    parser = argparse.ArgumentParser(description="Test Motors Service")
    parser.add_argument('--id', type=str, required=True, help='ID of the lamp')
    parser.add_argument('--port', type=str, required=True, help='Serial port for the lamp')
    args = parser.parse_args()
    
    print("Testing Motors Service...")
    
    motors_service = MotorsService(port=args.port, lamp_id=args.id)
    motors_service.start()
    
    try:
        print("Getting available recordings...")
        recordings = motors_service.get_available_recordings()
        print(f"Available recordings: {recordings}")
        
        if recordings:
            print(f"Playing first recording: {recordings[0]}")
            motors_service.dispatch("play", recordings[0])
            
            motors_service.wait_until_idle(timeout=30)
            print("Playback completed!")
        else:
            print("No recordings found. Create some recordings first.")
        
    finally:
        motors_service.stop()
        print("Motors Service test completed!")

if __name__ == "__main__":
    test_motors_service()