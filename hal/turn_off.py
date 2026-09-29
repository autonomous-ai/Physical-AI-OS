import sys
import os
import argparse

sys.path.append(os.path.dirname(__file__))

from service.rgb import RGBService
from follower import LeLampFollower, LeLampFollowerConfig
from hal.presets import RGB_CMD_SOLID

def turn_off(port: str, lamp_id: str):
    robot_config = LeLampFollowerConfig(port=port, id=lamp_id)
    robot = LeLampFollower(robot_config)
    
    rgb_service = RGBService()
    
    try:
        print(f"Connecting to robot on port {port} with ID {lamp_id}...")
        robot.connect(calibrate=False)
        print("Robot connected successfully")
        
        rgb_service.start()
        
        print("Turning off LED")
        rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        
        print("Turn off complete")
        
    except Exception as e:
        print(f"Error during turn off: {e}")
    finally:
        if robot.is_connected:
            print("Disconnecting robot...")
            robot.disconnect()
            print("Robot disconnected")
        
        rgb_service.stop()

def main():
    parser = argparse.ArgumentParser(description="Turn off LeLamp LED and disconnect robot")
    parser.add_argument('--id', type=str, required=True, help='ID of the lamp')
    parser.add_argument('--port', type=str, required=True, help='Serial port for the lamp')
    args = parser.parse_args()

    turn_off(args.port, args.id)

if __name__ == "__main__":
    main()
