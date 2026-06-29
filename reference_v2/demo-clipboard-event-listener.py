#!/usr/bin/env python3
"""
Clipboard event listener for X11 (Kubuntu/KDE).
Uses XFIXES for copy/WRITE events + xclip for content retrieval.

Usage:
  python3 demo-clipboard-event-listener.py
  python3 demo-clipboard-event-listener.py --primary   # also watch PRIMARY (noisy)

Requires: python3-xlib, xclip
Optional: Pillow (for image WxH detection)
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time
from io import BytesIO
from urllib.parse import unquote

from Xlib import display
from Xlib.ext import xfixes

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ---- helpers -----------------------------------------------------------------

def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    else:
        return f"{n / (1024 * 1024):.1f}MB"


def timestamp() -> str:
    now = time.time()
    ms = int((now - int(now)) * 1000)
    return time.strftime('%H:%M:%S') + f'.{ms:03d}'


def xclip(sel_name: str, target: str, timeout: float = 2.0):
    """Shell out to xclip to fetch selection content. Returns bytes or None."""
    try:
        p = subprocess.run(
            ['xclip', '-selection', sel_name.lower(), '-o', '-t', target],
            capture_output=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def get_targets(sel_name: str):
    """Fetch available target atom names for a selection."""
    raw = xclip(sel_name, 'TARGETS')
    if raw is None:
        return []
    return raw.decode('ascii', 'replace').split()


# Atoms that signal password-manager / secret content
PASSWORD_HINTS = {
    'x-kde-passwordManagerHint',
    'application/x-kde-passwordManagerHint',
}

TEXT_ATOMS = {
    'UTF8_STRING', 'STRING', 'TEXT',
    'text/plain', 'text/plain;charset=utf-8',
}

FILE_ATOMS = {
    'text/uri-list',
    'x-special/gnome-copied-files',
    'application/x-kde-cutselection',
}


def classify(targets):
    """Classify clipboard content based on available target atoms.

    Returns one of: 'PASSWORD', 'IMAGE', 'FILES', 'HTML', 'TEXT', 'OTHER'.
    """
    tset = set(targets)

    # 1. Password-manager redaction
    if tset & PASSWORD_HINTS:
        return 'PASSWORD'

    # 2. Image
    if any(t.startswith('image/') for t in targets):
        return 'IMAGE'

    # 3. File list
    if tset & FILE_ATOMS:
        return 'FILES'

    # 4. Rich text
    if 'text/html' in tset:
        return 'HTML'

    # 5. Plain text
    if tset & TEXT_ATOMS:
        return 'TEXT'

    return 'OTHER'


def pick_image_target(targets):
    """Pick the best image-type target from the available list."""
    for pref in ('image/png', 'image/jpeg', 'image/bmp', 'image/tiff'):
        if pref in targets:
            return pref
    for t in targets:
        if t.startswith('image/'):
            return t
    return 'image/png'


def format_line(sel_name: str, kind: str, targets):
    """Produce the fully-formatted output line for one clipboard event."""
    ts = timestamp()
    prefix = f"{ts} | WRITE | {sel_name} | "

    # -- PASSWORD (redacted) ---------------------------------------------------
    if kind == 'PASSWORD':
        return prefix + "TEXT <REDACTED: password-manager hint>"

    # -- TEXT / HTML -----------------------------------------------------------
    if kind in ('TEXT', 'HTML'):
        raw = (
            xclip(sel_name, 'UTF8_STRING')
            or xclip(sel_name, 'text/plain;charset=utf-8')
            or xclip(sel_name, 'STRING')
            or xclip(sel_name, 'text/plain')
            or b''
        )
        text = raw.decode('utf-8', 'replace')
        n_chars = len(text)
        n_bytes = len(raw)
        preview = text[:100].replace('\n', '\\n').replace('\t', '\\t')
        more = f" (+{n_chars - 100} more chars)" if n_chars > 100 else ""
        return (prefix + f"TEXT chars={n_chars} bytes={n_bytes}  "
                f"preview=\"{preview}\"{more}")

    # -- IMAGE -----------------------------------------------------------------
    if kind == 'IMAGE':
        img_target = pick_image_target(targets)
        raw = xclip(sel_name, img_target, timeout=5.0)
        if raw is None or len(raw) == 0:
            return prefix + "IMAGE <unavailable>"
        n_bytes = len(raw)
        wh = ""
        if HAS_PIL:
            try:
                img = Image.open(BytesIO(raw))
                wh = f" {img.width}x{img.height}"
            except Exception:
                pass
        fmt = img_target.split('/')[-1].upper()
        return prefix + f"IMAGE {fmt} {human_bytes(n_bytes)}{wh}"

    # -- FILES -----------------------------------------------------------------
    if kind == 'FILES':
        raw = xclip(sel_name, 'text/uri-list')
        if raw is None:
            return prefix + "FILES <unavailable>"
        text = raw.decode('utf-8', 'replace')
        uris = [l for l in text.splitlines() if l and not l.startswith('#')]
        # Parse file:// URIs
        paths = []
        for u in uris:
            if u.startswith('file://'):
                paths.append(unquote(u[7:]))
        if not paths:
            n = len(uris)
            return prefix + f"FILES count={n}  {', '.join(uris[:3])}"
        n = len(paths)
        display_paths = "; ".join(paths[:5])
        tail = " ..." if n > 5 else ""
        return prefix + f"FILES count={n}  {display_paths}{tail}"

    # -- OTHER -----------------------------------------------------------------
    raw = xclip(sel_name, 'UTF8_STRING')
    n_bytes = len(raw) if raw else 0
    return (prefix + f"OTHER targets=[{' '.join(sorted(set(targets)))}]  "
            f"bytes={n_bytes}")


# ---- main -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Clipboard event listener (XFIXES + xclip).")
    parser.add_argument('--primary', action='store_true',
                        help='Also watch PRIMARY selection (chatty — every text highlight)')
    args = parser.parse_args()

    # Unbuffered stdout so piped/redirected output is line-buffered
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)

    # --- Environment checks ---------------------------------------------------
    if os.environ.get('XDG_SESSION_TYPE', '') != 'x11':
        print("error: X11 session required (currently not X11)", file=sys.stderr)
        sys.exit(1)

    if subprocess.run(['which', 'xclip'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        print("error: xclip not found. Install with: sudo apt install xclip",
              file=sys.stderr)
        sys.exit(1)

    # --- X11 setup ------------------------------------------------------------
    dpy = display.Display()
    root = dpy.screen().root

    if 'XFIXES' not in dpy.list_extensions():
        print("error: XFIXES extension not available", file=sys.stderr)
        sys.exit(1)
    dpy.xfixes_query_version()

    # Register for selection-owner-change events (CLIPBOARD + optional PRIMARY)
    selections = {dpy.intern_atom('CLIPBOARD'): 'CLIPBOARD'}
    if args.primary:
        selections[dpy.intern_atom('PRIMARY')] = 'PRIMARY'
        print("(PRIMARY selection monitoring enabled — may be noisy)",
              file=sys.stderr)

    mask = xfixes.XFixesSetSelectionOwnerNotifyMask
    for sel in selections:
        dpy.xfixes_select_selection_input(root, sel, mask)
    dpy.flush()

    # The XFIXES extension event code for SetSelectionOwnerNotify is (base, sub_code)
    ev_base = dpy.extension_event.SetSelectionOwnerNotify[0]

    # --- Event loop -----------------------------------------------------------
    last_hash = {}  # sel_name -> md5 hexdigest of last content

    print(f"Listening for clipboard changes (PID {os.getpid()})",
          file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    try:
        while True:
            ev = dpy.next_event()

            if ev.type != ev_base:
                continue

            sel_name = selections.get(getattr(ev, 'selection', None))
            if sel_name is None:
                continue

            # Fetch available targets
            targets = get_targets(sel_name)
            if not targets:
                continue

            # Classify
            kind = classify(targets)

            # Skip PASSWORD (redacted) — no content fetch needed
            if kind == 'PASSWORD':
                print(format_line(sel_name, kind, targets), flush=True)
                continue

            # Get a content digest for dedup
            if kind == 'TEXT' or kind == 'HTML':
                content_bytes = (xclip(sel_name, 'UTF8_STRING')
                                 or xclip(sel_name, 'text/plain;charset=utf-8')
                                 or xclip(sel_name, 'STRING')
                                 or xclip(sel_name, 'text/plain')
                                 or b'')
            elif kind == 'IMAGE':
                ct = pick_image_target(targets)
                content_bytes = xclip(sel_name, ct, timeout=5.0) or b''
            elif kind == 'FILES':
                content_bytes = xclip(sel_name, 'text/uri-list') or b''
            else:
                content_bytes = xclip(sel_name, 'UTF8_STRING') or b''

            h = hashlib.md5(content_bytes).hexdigest()
            if last_hash.get(sel_name) == h:
                continue  # dedup — same content as last event
            last_hash[sel_name] = h

            print(format_line(sel_name, kind, targets), flush=True)

    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


if __name__ == '__main__':
    main()
