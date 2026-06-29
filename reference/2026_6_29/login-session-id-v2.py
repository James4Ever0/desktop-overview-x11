#!/usr/bin/env python3
"""
session_id_extended.py

Generate a unique session identifier for the current graphical login session.
Prints:
- Boot ID
- Boot time (human-readable + epoch)
- Session start time
- Session user name
- Session user ID (UID)
- MD5 hash of boot_id + ":" + session_start_time
"""

import os
import subprocess
import hashlib
import sys
import time as time_module  # for time formatting

def get_boot_id():
    """Read system boot ID from /proc/sys/kernel/random/boot_id."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        sys.exit("Error: /proc/sys/kernel/random/boot_id not found (not a Linux system?).")

def get_boot_time():
    """Retrieve system boot time from /proc/stat (btime field)."""
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime"):
                    # btime is the second token on the line
                    parts = line.split()
                    if len(parts) >= 2:
                        epoch = int(parts[1])
                        return epoch
                    break
        sys.exit("Error: btime not found in /proc/stat.")
    except FileNotFoundError:
        sys.exit("Error: /proc/stat not found.")
    except ValueError:
        sys.exit("Error: Failed to parse btime from /proc/stat.")

def get_session_start_time(session_id):
    """Retrieve session start timestamp from loginctl."""
    try:
        result = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", "Timestamp", "--value"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit(f"Error: Failed to query logind for session {session_id}. Are you in a logind session?")
    except FileNotFoundError:
        sys.exit("Error: 'loginctl' command not found. Is systemd-logind installed?")

def get_session_user_name(session_id):
    """Retrieve user name of the session owner."""
    try:
        result = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", "Name", "--value"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit(f"Error: Failed to get user name for session {session_id}.")
    except FileNotFoundError:
        sys.exit("Error: 'loginctl' command not found.")

def get_session_user_uid(session_id):
    """Retrieve UID (numeric) of the session owner."""
    try:
        result = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", "User", "--value"],
            capture_output=True, text=True, check=True
        )
        uid = result.stdout.strip()
        # The 'User' property returns the numeric UID string
        return uid
    except subprocess.CalledProcessError:
        sys.exit(f"Error: Failed to get UID for session {session_id}.")
    except FileNotFoundError:
        sys.exit("Error: 'loginctl' command not found.")

def main():
    # 1. Boot ID
    boot_id = get_boot_id()

    # 2. Boot time (epoch)
    boot_epoch = get_boot_time()
    boot_time_human = time_module.strftime("%Y-%m-%d %H:%M:%S %Z", time_module.localtime(boot_epoch))

    # 3. Session ID
    session_id = os.environ.get("XDG_SESSION_ID")
    if not session_id:
        sys.exit("Error: XDG_SESSION_ID not set. Are you running inside a logind session?")

    # 4. Session start time
    session_start = get_session_start_time(session_id)

    # 5. User info
    user_name = get_session_user_name(session_id)
    user_uid = get_session_user_uid(session_id)

    # 6. Build hash key: boot_id + ":" + session_start
    hash_input = f"{boot_id}:{session_start}"
    md5_hash = hashlib.md5(hash_input.encode("utf-8")).hexdigest()

    # Output
    print(f"Boot ID:            {boot_id}")
    print(f"Boot Time:          {boot_time_human} (epoch: {boot_epoch})")
    print(f"Session Start Time: {session_start}")
    print(f"Session User Name:  {user_name}")
    print(f"Session User ID:    {user_uid}")
    print(f"MD5 Hash:           {md5_hash}")

if __name__ == "__main__":
    main()
