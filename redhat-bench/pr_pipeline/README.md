# PR Validation Pipeline

Creates SWE-bench instances from real merged pull requests and converts them to Harbor format. Works for Go, Python, Java, and C (PCP) repos — no SWE-Smith profiles needed.

## Pipeline Flow

```
collect_prs.py → validate_prs.py → generate_issues.py → generate_harbor.py → merge_dataset.py
     ↓                 ↓                  ↓                    ↓                    ↓
  Scrape PRs      Docker 0→1         LLM problem         Harbor tasks          Merge into
  with tests      validation         statements           + gold.patch          dataset
```

## Directory Structure

```
pr_pipeline/
  pipeline/                    — core scripts (language-agnostic)
    collect_prs.py             — Step 1: scrape merged PRs from GitHub
    validate_prs.py            — Step 2: Docker-based 0→1 validation
    generate_harbor.py         — Step 3: convert to Harbor format
    generate_issues.py         — Step 4: LLM problem statements + difficulty
    merge_dataset.py           — Step 5: dedup and merge into dataset
    validate_harbor.sh         — Harbor task validation script
  configs/                     — repo configurations
    go_python_repos.json       — Go/Python/Java repo list (25 repos)
    pcp_repos.json             — PCP (C language) repo config
  harness/                     — language-specific harnesses
    pcp/                       — C/PCP custom harness
      Dockerfile               — PCP build environment
      setup_pcp.sh             — per-instance setup (checkout, build)
      eval_pcp.sh              — per-instance evaluation (rebuild, run qa)
      validate_pcp.py          — PCP validation script
      instances.json           — PCP instance configs
    python/                    — Python env generation
      auto_env.py              — auto dependency resolution
  data/                        — collected PR data
    pcp_prs.jsonl              — collected PCP PRs
```

## Prerequisites

```bash
pip install docker unidiff requests openai
```

- Docker daemon running
- `GITHUB_TOKEN` for PR collection: `export GITHUB_TOKEN=$(gh auth token)`
- `OPENAI_API_KEY` for issue generation

## Quick Start

```bash
cd pr_pipeline

# 1. Collect PRs
export GITHUB_TOKEN=$(gh auth token)
python pipeline/collect_prs.py configs/go_python_repos.json data/collected_prs.jsonl --max-pulls 200

# 2. Validate (Docker 0→1 check)
python pipeline/validate_prs.py data/collected_prs.jsonl --lang go --output-dir ./results --timeout 600

# 3. Generate problem statements + difficulty
export OPENAI_API_KEY=your-key
python pipeline/generate_issues.py ./results --output data/instances_rated.json

# 4. Convert to Harbor format
python pipeline/generate_harbor.py --input data/instances_rated.json --output ../harbor-dataset-final

# 5. Merge into dataset
python pipeline/merge_dataset.py data/instances_rated.json --dataset ../harbor-dataset-final/rh_swe_bench_verified.json
```

## How Each Step Works

### Step 1: Collect PRs (`collect_prs.py`)

Scans merged PRs via GitHub API and keeps ones that change BOTH code AND test files:

```
For each merged PR:
  files = GET /pulls/{number}/files
  has_test = any file matches test pattern? (_test.go, test_*.py, Test.java)
  has_code = any file is NOT a test?
  if both → split diff into code_files (patch) + test_files (test_patch)
```

### Step 2: Validate (`validate_prs.py`)

For each collected PR, builds a Docker container and checks 0→1:

```
1. Clone repo at base_commit (pre-fix, has the bug)
2. Apply test_patch (add tests that catch the bug)
3. Run tests → should FAIL (0)
4. Apply patch (the code fix)
5. Run tests → should PASS (1)
6. 0→1 = VALID
```

Language-specific test commands:
| Language | Test Command | Parser |
|----------|-------------|--------|
| Go | `go test -v -short -count=1 -run "TestFoo" ./pkg/...` | `parse_go_log` |
| Python | `python -m pytest --disable-warnings test_file.py::test_func` | `parse_pytest_log` |
| Java | `mvn test -B -Dtest=ClassName` | `parse_maven_log` |

### Step 3: Generate Harbor (`generate_harbor.py`)

Converts validated instances to Harbor task format:

```
task-NNNN/
  environment/
    Dockerfile          — repo clone + checkout + dependencies + patches
    setup.patch         — bug introduction (synthetic) or test.patch (PR)
  tests/
    test.sh             — runs F2P tests, writes 1/0 to reward.txt
  solution/
    solve.sh            — base64-encoded gold patch (self-contained)
    gold.patch          — readable patch for inspection
  task.toml             — metadata (difficulty, timeout)
  instruction.md        — problem statement
  input.yaml            — problem statement (YAML)
```

Key features:
- **Go**: uses `-run` flag + exit code (not grep) for test checking
- **Go (go.work repos)**: uses `;` between module test runs (not `&&`) to prevent short-circuiting
- **Python (conda)**: removes `defaults` channel + disables ssl_verify to fix Anaconda SSL errors
- **Python (non-conda)**: adds `pip install pytest` as fallback
- **Python test args**: uses single quotes to protect `$`, `#`, `&` in parametrized test names
- **PCP (C)**: separate harness with privileged Docker, pmcd setup, PMDA reinstall

## Supported Languages

| Language | Collect | Validate | Harbor | Status |
|----------|---------|----------|--------|--------|
| Go | ✅ | ✅ | ✅ | 382 validated instances |
| Python | ✅ | ✅ | ✅ | 124 validated instances |
| C (PCP) | Custom | Custom | ✅ | 8 validated instances |
| Java | ✅ | ✅ | ❌ | No Dockerfile template yet |

## Adding a New Repo

Add to `configs/go_python_repos.json`:

```json
{"repo": "your-org/your-repo", "lang": "python"}
```

For PCP/C repos, use `configs/pcp_repos.json` and the custom harness in `harness/pcp/`.

## Expected Yields

| Language | Repos | Yield |
|----------|-------|-------|
| Go | prometheus, kustomize, rook, router, CVO | 20-45% |
| Go | etcd, buildah, podman | 0-5% |
| Python | insights-core | ~50% |
| Python | molecule, ansible, osbuild | 15-30% |
| C (PCP) | performancecopilot/pcp | ~80% |

Yield = validated instances / collected PRs. Depends on test-code coupling.

## Running on OpenShift / NERC

```bash
# Create privileged DinD pod
oc apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: pr-validator
spec:
  priorityClassName: openshift-user-critical
  containers:
  - name: runner
    image: docker:24-dind
    command: ["sh", "-c", "dockerd-entrypoint.sh & sleep 5 && sleep infinity"]
    securityContext:
      privileged: true
    volumeMounts:
    - name: workspace
      mountPath: /workspace
  volumes:
  - name: workspace
    persistentVolumeClaim:
      claimName: validator-pvc
EOF

# Upload and run
oc cp pipeline/ pr-validator:/workspace/pipeline -c runner
oc cp data/ pr-validator:/workspace/data -c runner
oc exec pr-validator -c runner -- sh -c '
  cd /workspace
  python3 pipeline/validate_prs.py data/collected_prs.jsonl --lang go --output-dir results
'
```

## Output Format

Each validated instance follows the SWE-bench format:

| Field | Description |
|-------|-------------|
| `instance_id` | `owner__repo.pr_NUMBER_v3` |
| `repo` | `owner/repo` |
| `base_commit` | Git SHA at the PR's base |
| `patch` | Code changes (the fix) |
| `test_patch` | Test changes from the PR |
| `problem_statement` | LLM-generated bug report |
| `FAIL_TO_PASS` | Tests that fail without fix, pass with fix |
| `PASS_TO_PASS` | Tests that pass both before and after |
| `difficulty` | LLM-rated: <15 min, 15min-1hr, 1-4hrs, >4hrs |
| `bug_type` | `pr_direct` for PR instances |
| `repo_language` | Go, Python, C |
| `rh_product` | Red Hat product mapping |
