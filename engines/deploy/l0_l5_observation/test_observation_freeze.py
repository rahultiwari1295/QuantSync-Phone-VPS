import hashlib
import re
from pathlib import Path

def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

def test_observation_artifacts_unchanged():
    """All artifacts listed in frozen_hashes.md must be bit-identical."""
    hash_file = Path('deploy/l0_l5_observation/frozen_hashes.md')
    content = hash_file.read_text()
    
    failures = []
    for line in content.splitlines():
        match = re.match(r'- `([^`]+)` — SHA256: `([a-f0-9]{64})`', line.strip())
        if not match:
            continue
        path, expected = match.groups()
        if not Path(path).exists():
            failures.append(f"MISSING: {path}")
            continue
        actual = sha256(path)
        if actual != expected:
            failures.append(f"CHANGED: {path}\n  expected {expected}\n  got      {actual}")
    
    assert not failures, "Frozen artifacts have changed:\n" + "\n".join(failures)

if __name__ == '__main__':
    test_observation_artifacts_unchanged()
    print("All observation artifacts verified unchanged (SHA-256 match).")
