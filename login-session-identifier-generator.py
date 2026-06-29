#!/usr/bin/env python3
"""
session_id.py - Generate a unique identifier for the current graphical login session.

Uses:
- Boot ID from /proc/sys/kernel/random/boot_id
- Session start timestamp from logind (via loginctl)
- MD5 hash of boot_id + timestamp

Outputs boot_id, session start time, and the hash.
"""

import os
import subprocess
import hashlib
import sys

def get_boot_id():
    """Read the system boot ID."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        sys.exit("Error: /proc/sys/kernel/random/boot_id not found (not a Linux system?).")

def get_session_start_time(session_id):
    """Retrieve the session start timestamp from loginctl."""
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

def main():
    # Get boot ID
    boot_id = get_boot_id()

    # Get session ID from environment (set by logind)
    session_id = os.environ.get("XDG_SESSION_ID")
    if not session_id:
        sys.exit("Error: XDG_SESSION_ID not set. Are you running inside a logind session?")

    # Get session start timestamp
    start_time = get_session_start_time(session_id)

    # Build a combined string: boot_id + ":" + start_time
    combined = f"{boot_id}:{start_time}"

    # Compute MD5 hash
    md5_hash = hashlib.md5(combined.encode("utf-8")).hexdigest()

    # Output results
    print(f"Boot ID:            {boot_id}")
    print(f"Session Start Time: {start_time}")
    print(f"MD5 Hash:           {md5_hash}")

if __name__ == "__main__":
    main()
