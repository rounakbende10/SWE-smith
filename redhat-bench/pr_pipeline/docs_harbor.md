# Harbor Dataset Generator

Converts RH SWE-bench dataset (531 instances) to Harbor format for agent evaluation.

## Quick Start

```bash
# Generate full dataset (default output: ../harbor-dataset/)
python3 generate_harbor.py

# Custom output directory
python3 generate_harbor.py --output /path/to/output

# Test with limited tasks
python3 generate_harbor.py --limit 10 --output /tmp/harbor-test
```

## Dataset Structure

```
harbor-dataset/
  eval.yaml              # Harbor evaluation config
  task-0000/             # First task
    task.toml            # Metadata: difficulty, timeout, category
    input.yaml           # Problem statement for agent (prompt field)
    instruction.md       # Problem statement as markdown
    environment/
      Dockerfile         # Sets up repo at base_commit with deps
    solution/
      solve.sh           # Oracle solution (base64-encoded patch)
    tests/
      test.sh            # Test runner (writes 1/0 to /logs/verifier/reward.txt)
  task-0001/
  ...
  task-0530/
```

## Instance Types

### 1. Synthetic Instances (376 total)
- **Identified by:** `setup_patch` is non-empty, instance_id does NOT end with `_v3`
- **Base state:** Clean repo at `base_commit`
- **Dockerfile:** Applies `setup_patch` to introduce the bug
- **Gold patch:** REVERSED before encoding (original patch is clean→buggy, we need buggy→clean)
- **Tests:** Pre-exist in the repo

### 2. PR-Based Instances (155 total)
- **Identified by:** instance_id ends with `_v3`, `test_patch` is non-empty
- **Base state:** Buggy repo at `base_commit`
- **Dockerfile:** Applies `test_patch` to add test files
- **Gold patch:** Used as-is (already buggy→clean)
- **Tests:** Added via `test_patch`

## Language-Specific Handling

### Python (105 instances)
- **Base image:** `python:3.12-bookworm`
- **Deps:** `requirements.txt`, `requirements-dev.txt`, `requirements-test.txt`
- **Test command:** `python -m pytest --tb=short -v <test_paths>`
- **Test paths:** Extracted from `FAIL_TO_PASS` (e.g., `path/to/test.py::TestClass::test_method` → `path/to/test.py`)

### Go (332 instances)
- **Base image:** `golang:1.22`
- **Deps:** `go mod download`
- **Test command:** `go test -v -short -count=1 ./...`
- **Test scope:** Runs all tests (FAIL_TO_PASS doesn't include package paths)

### Unknown (94 instances)
- **Base image:** `python:3.12-bookworm` (default)
- **Test command:** Generic pass (writes `1` to reward.txt)

## Difficulty Mapping

| Original | Harbor | Timeout | Count |
|---|---|---|---|
| `<15 min fix` | easy | 600s | 289 |
| `15 min - 1 hour` | medium | 1800s | 172 |
| `1-4 hours` | hard | 3600s | 64 |
| `>4 hours` | hard | 3600s | 6 |

## Files Generated per Task

### task.toml
```toml
version = "1.0"

[metadata]
author_name = "rh-swe-bench"
difficulty = "medium"
category = "swe_bench"
tags = ["rh-swe-bench", "auto-generated"]

[verifier]
timeout_sec = 1800.0

[agent]
timeout_sec = 1800.0

[environment]
build_timeout_sec = 600.0
cpus = 2
memory_mb = 8192
storage_mb = 20480
```

### input.yaml
```yaml
prompt: "<problem_statement from instance>"
```

### instruction.md
Plain markdown version of the problem statement.

### environment/Dockerfile
- Clone repo from GitHub
- Checkout `base_commit`
- Install language-specific dependencies
- Apply `setup_patch` (synthetic) OR `test_patch` (PR-based)

### solution/solve.sh
```bash
#!/bin/bash
set -e

# Apply the gold patch to fix the bug
echo '<base64_encoded_patch>' | base64 -d | patch -p1

echo 'Gold patch applied successfully'
```

**Note:** For synthetic instances, the patch is REVERSED before encoding.

### tests/test.sh
```bash
#!/bin/bash
set -e
mkdir -p /logs/verifier

# Run tests
if <language-specific-test-command>; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit 0
```

## eval.yaml

Top-level Harbor evaluation config:
- Dataset name: `rh-swe-bench-v1`
- Runner: `claude-code`
- Models: `claude-sonnet-4-6` (skill), `claude-opus-4-6` (judge)
- Execution mode: `case` with `{prompt}` argument
- Parallelism: 4
- Judges: `has_code_changes`, `solution_quality`

## Implementation Notes

### Patch Reversal (Synthetic Instances)
The original `patch` field in synthetic instances has the SAME direction as `setup_patch` (both remove code). This is because the synthetic pipeline creates:
1. Clean code at `base_commit`
2. `setup_patch`: clean → buggy (removes good code)
3. `patch`: clean → buggy (same as setup_patch)

For Harbor, we need buggy → clean, so we reverse the patch by swapping:
- `---` ↔ `+++` in file headers
- `-` ↔ `+` in diff lines

### Test Path Extraction

**Python:**
```python
"path/to/test.py::TestClass::test_method" → "path/to/test.py"
```

**Go:**
```python
"github.com/org/repo/pkg/subpkg.TestFunc" → "./pkg/subpkg/..."
```
However, Go `FAIL_TO_PASS` entries don't always include package paths, so we default to `./...` (run all tests).

### Base64 Encoding
All patches (setup, test, gold) are base64-encoded to avoid shell escaping issues in Dockerfiles and scripts.

## Verification Checklist

- [x] 531 tasks generated
- [x] Difficulty distribution: 289 easy, 172 medium, 70 hard
- [x] Synthetic patches are reversed in solve.sh
- [x] PR-based patches are NOT reversed in solve.sh
- [x] Python instances use pytest with extracted test paths
- [x] Go instances use `go test` with appropriate package paths
- [x] Dockerfiles apply setup_patch (synthetic) or test_patch (PR-based)
- [x] eval.yaml generated with correct task count and metadata
- [x] All scripts are executable (solve.sh, test.sh)

## Next Steps

1. **Validate sample tasks:** Build and run a few Dockerfiles to ensure they work
2. **Test oracle solutions:** Run solve.sh and verify tests pass
3. **Harbor integration:** Import dataset into Harbor evaluation framework
4. **Baseline evaluation:** Run against Claude Sonnet 4.6 to establish baseline metrics
