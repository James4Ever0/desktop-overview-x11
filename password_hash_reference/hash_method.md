To detect and replace a known sensitive string (e.g., a password) in a captured keystroke stream using only its hash, you need to answer a key design question: **What information is necessary to locate the original string in the data?** The hash alone tells you *whether* a specific string matches, but not *which* substring to replace, unless you test every possible substring. That’s why storing auxiliary data like length or a prefix is crucial for efficiency.

### Why you **need** the string length
- If you store only `hash(sensitive_string)`, then to check if any substring in the captured text matches, you must compute the hash on every substring of every possible length → `O(n²)` hash computations for a text of length `n`, which is impractical.
- Storing the **length** alongside the hash lets you slide a window of exactly that length, compute the hash once per window, and compare – only `O(n)` hashes. This is the minimal requirement for a workable sliding-window detector.
- **Recommendation**: Store `(hash, length)` for each sensitive entry.

### Optional: prefix character(s)
Storing the first 1–2 characters as an index key can accelerate lookups. You can group hashes by first character, so when scanning a captured string you only test windows whose first character matches one of the prefix groups. This reduces the number of hash computations, especially if you have many sensitive strings. It is **not necessary**, but useful at scale.

### Do you need the actual sensitive string?
No – by design you only store its hash. When you find a window whose hash matches a stored hash, you replace that window with a placeholder like `[REDACTED]`. You never recover the original string, which is the whole point.

### Efficient approach
1. **Registration phase**: When a sensitive string is first seen, compute `h = hash(s)` and store `(h, len(s), s[0])` in a local database/file.
2. **Detection phase**: For each captured keystroke log (plain text):
   - For each stored entry with length `L` and first char `c`, slide a window of length `L` over the text.
   - Only compute the full hash if the window’s first character equals `c` (and optionally use a fast bloom filter for further pre-filtering).
   - If hash matches, overwrite that window with the placeholder.

### Existing solutions & tools
No widely known open-source tool does exactly “hash-only sensitive string redaction for arbitrary keyboard captures” out of the box, but several components can be combined or adapted:

- **Microsoft Presidio** (Python) – A data anonymization framework that detects sensitive data (credit cards, names, etc.) using patterns, NLP, and custom recognizers. You can write a custom recognizer that uses a hash set + lengths to detect known secrets.
- **Google Cloud DLP** – Can de-identify structured/unstructured text by matching against custom dictionaries, though it typically works with plaintext patterns, not hashes.
- **Hashicorp Vault Transform** (Tokenization) – Transforms sensitive data into tokens; you could tokenize the original string and later scan for the token, but this requires the token to be present in the stream – not a generic hash detection.
- **Log sanitizers** (e.g., `log-sanitizer`, `fluentd` filter plugins) – Often support regex-based redaction. You could pre-compute a regex that matches any of the known strings, but that would expose the plaintext in the configuration (defeating the purpose of hashing). Not recommended.
- **Keyloggers with exclusion lists** (e.g., `logkeys` for Linux) allow you to ignore certain keystrokes but don’t hash.
- **Custom script** – A simple Python/Go/Rust script with a hash table `{(length, first_char, hash) -> placeholder}` is trivial to write and offers full control. Use a fast non-cryptographic hash (xxHash, FarmHash) if collision resistance is not paramount, or a cryptographic one (SHA-256) if security matters.

### Important security considerations
- **Hash collisions**: If you use a fast hash, two different strings might produce the same hash, leading to false redaction. Use a cryptographic hash if the system must be robust against adversarial input.
- **Salting?** If the sensitive strings are user‑provided and you fear rainbow‑table attacks against the hash file, you can salt the hash, but then you must store the salt per entry and incorporate it during scanning. That’s rarely needed if the hash file itself is properly protected (file permissions, encryption).
- **Partial window attacks**: If an attacker knows the hash file, they could brute‑force short strings offline. Mitigate by enforcing a minimum length for sensitive strings and using a strong hash function with iteration (e.g., PBKDF2) if necessary.

### Final answer
**Store the hash together with the string length.** A one‑character prefix is a useful performance index but not mandatory. If you implement it yourself, a simple sliding‑window approach with length‑indexed lookups is straightforward and effective. For existing libraries, Microsoft Presidio’s custom recognizer is the closest adaptable solution; otherwise, a small custom script is the most direct path.