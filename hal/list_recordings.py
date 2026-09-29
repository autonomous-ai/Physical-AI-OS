import argparse
import os
import glob
import csv
from datetime import datetime


def list_recordings(lamp_id):
    """List all recordings for a given lamp ID."""
    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    
    if not os.path.exists(recordings_dir):
        print(f"No recordings directory found at {recordings_dir}")
        return
    
    pattern = os.path.join(recordings_dir, f"*.csv")
    recording_files = glob.glob(pattern)
    
    if not recording_files:
        print(f"No recordings found for lamp ID: {lamp_id}")
        return
    
    print(f"Recordings for lamp ID '{lamp_id}':")
    print()
    
    for file_path in sorted(recording_files):
        filename = os.path.basename(file_path)
        recording_name = filename.replace(f".csv", "")
        
        try:
            with open(file_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                row_count = sum(1 for row in reader) - 1  # Subtract header row
        except Exception as e:
            row_count = "Error reading file"
        
        stat = os.stat(file_path)
        modified_time = datetime.fromtimestamp(stat.st_mtime)
        
        print(f"{recording_name}")
        print(f"  File: {filename}")
        print(f"  Rows: {row_count}")
        print()


def main():
    parser = argparse.ArgumentParser(description="List recordings for a specific lamp ID")
    parser.add_argument('--id', type=str, required=True, help='ID of the lamp to list recordings for')
    args = parser.parse_args()
    
    list_recordings(args.id)


if __name__ == "__main__":
    main()