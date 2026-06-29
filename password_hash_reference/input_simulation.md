Handling backspaces and partial input is absolutely possible, but it introduces extra complexity. The core idea: **don’t match against raw keystroke events—first reconstruct the effective text buffer, then apply your hash‑based detection.**  

### 1. Reconstruct the effective text
Maintain a mutable string buffer (like a text field in memory) that updates with each keystroke:

- **Printable character** → append to buffer.
- **Backspace** → remove the last character (if any).
- **Delete / arrow keys / Home / End** → if you capture these, simulate their effect on cursor position and deletion.
- **Enter / Tab / focus loss** → optional commit point.

After each event you have the exact string that would be visible in an input field. Run your sliding‑window hash detection *on this buffer*. When a hash matches, you can overwrite that substring with `[REDACTED]` right in the buffer, preventing any further logging of the secret.

### 2. Sanitizing the keystroke log itself
If your goal is to sanitize the *keystroke log* (the stream of key codes), you need to map buffer characters back to the keystrokes that produced them. This is doable:

- Keep a parallel list: for each character in the buffer, store the set of keystrokes responsible for it (the one that inserted it, minus any backspaces that later removed it).
- When you detect a sensitive string in the buffer, find those keystrokes and replace them in the log with a placeholder event (e.g., `[REDACTED_KEY]`) or simply omit them.

However, if you only need the *final* text (not a raw key log), it’s simpler: just redact the buffer and log the sanitized text at commit points. Most practical key‑capture tools that need to remove secrets work this way—they log what was actually entered, not every physical keystroke.

### 3. The tricky part: partial credentials and backspace
Even with reconstruction, hash‑based matching only catches **complete, exact strings**. If a user types `p`,`a`,`s`,`s` (forming "pass") then backspaces twice and types `word`, the buffer never contains the full password `password`—only the fragments `pass` and `word`. Neither matches the stored hash, so the fragment `pass` leaks into the log.  

**This is an inherent limitation of using hashes alone.** To catch fragments you’d need the plaintext (or a set of hashes of all prefixes, which would be enormous and still leak information). Possible mitigations:

- **Log only at stable moments** – Instead of logging every keystroke, capture the buffer after a short idle period (e.g., 2 seconds of no typing) or when the field loses focus. Transient fragments are never recorded.
- **Use OS‑level secure input** – On many systems, password fields mark the input as “secure” and the OS prevents standard keyloggers from capturing it. If you can rely on that, the problem disappears.
- **Accept the residual risk** – For many environments (e.g., internal monitoring), occasional short fragments that don’t form the complete secret are an acceptable trade‑off for the security of not storing plaintext.
- **Hybrid approach** – If you absolutely must catch fragments, you must store the actual sensitive strings (not just hashes) and scan continuously. This is less secure but sometimes unavoidable. You can still encrypt the list at rest.

### 4. Practical recommendations
- **Store `(hash, length)`** as originally advised.  
- **Add a commit‑based logging strategy**: log only after Enter, Tab, or a short inactivity timeout, not on every keypress.  
- **If you must capture raw keystrokes**, implement the buffer‑provenance mapping described above. It’s not trivial but well within reach for a custom C/Python/Rust tool.
- **Existing tools**: I’m not aware of a ready‑made open‑source library that does all of this out of the box. You’d likely combine a key‑capture module (e.g., `pynput`, `evdev`) with your own buffer logic and hash matcher.

In short: **yes, there is a fix**, but you need to simulate the text field and carefully design *when* you log to avoid capturing transient fragments.