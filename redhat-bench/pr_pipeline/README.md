# PR Validation Pipeline

Creates SWE-bench instances from real merged pull requests. Works for Go, Python, and Java repos — no SWE-Smith profiles needed.

## How it works

1. **Collect** merged PRs that change both code and test files
2. **Validate** each PR by building a Docker image at the PR's base commit, applying the test changes, running tests (should fail), applying the fix, running tests again (should pass)
3. **Generate** issue text and difficulty ratings using an LLM
4. **Merge** valid instances into the dataset

For Python repos, `auto_env.py` handles dependency resolution automatically — it reads the repo's config, downloads the right wheels, and builds an offline Docker image.

## Prerequisites

```bash
pip install swesmith docker unidiff requests openai
```

- Docker daemon running (local Docker, Podman, or remote via `DOCKER_HOST`)
- `GITHUB_TOKEN` for PR collection (`export GITHUB_TOKEN=$(gh auth token)`)
- `OPENAI_API_KEY` for issue generation

## Quick start

```bash
cd redhat-bench/pr_pipeline

# 1. Collect PRs
export GITHUB_TOKEN=$(gh auth token)
python collect_prs.py repos.json collected_prs.jsonl --max-pulls 200

# 2. Validate
python validate_prs.py collected_prs.jsonl --lang python --output-dir ./results --timeout 600

# 3. Generate issue text + difficulty rating
export OPENAI_API_KEY=your-key
python generate_issues.py ./results --output instances_rated.json

# 4. Merge into dataset
python merge_dataset.py instances_rated.json --dataset ../path/to/dataset.json
```

## What happens under the hood

### Go repos
Straightforward — `go mod download` and `go test` work in any Docker container with internet.

### Python repos (`auto_env.py`)
DinD containers often can't reach PyPI reliably, so the pipeline:

1. **Clones** the repo on the runner (which has internet)
2. **Reads** setup.cfg / pyproject.toml / setup.py to detect available extras (`[test]`, `[testing]`, `[dev]`, etc.)
3. **Downloads** platform-specific wheels (`manylinux2014_x86_64`, `cp312`) on the runner
4. **Builds** a Docker image with a fat base (common system libs pre-installed) + offline `pip install --no-index`
5. **Detects** the test runner (pytest / unittest / tox)
6. **Caches** the base image — subsequent PRs from the same repo build in seconds

No per-repo configuration needed. Just `{"repo": "owner/name", "lang": "python"}` in `repos.json`.

### Java repos
Uses Maven with test class scoping (`-Dtest=ClassName`). Apply both test_patch and fix first (Java test code may depend on fix code), then revert fix for buggy-state testing.

## Adding a new repo

Add it to `repos.json`:

```json
{"repo": "your-org/your-repo", "lang": "python"}
```

Then run the pipeline. `auto_env.py` will figure out the rest:
- What extras to install
- What test runner to use
- What system packages are needed

## Running at scale on OpenShift / NERC

```bash
# Upload scripts + data to a pod
oc cp validate_prs.py <namespace>/<pod>:/tmp/validate_prs.py -c runner
oc cp auto_env.py <namespace>/<pod>:/tmp/auto_env.py -c runner
oc cp collected_prs.jsonl <namespace>/<pod>:/tmp/batch.jsonl -c runner

# Start validation in background
oc exec <pod> -c runner -- bash -c '
  export DOCKER_HOST=tcp://localhost:2375
  export PYTHONPATH=/tmp
  nohup python3 -u /tmp/validate_prs.py /tmp/batch.jsonl --lang python \
    --output-dir /workspace/SWE-smith/logs/v3_validation > /tmp/v3_run.log 2>&1 &
'

# Monitor
oc exec <pod> -c runner -- tail -f /tmp/v3_run.log
oc exec <pod> -c runner -- grep -c "\[VALID\]" /tmp/v3_run.log
```

## Expected yields

| Language | Repos | Yield |
|----------|-------|-------|
| Go | prometheus, kustomize, rook, router, CVO | 20-45% |
| Go | etcd, buildah, podman | 0-5% |
| Python | insights-core | ~50% |
| Python | molecule, ansible, osbuild | TBD |
| Java | wildfly | 0% (multi-module compile issues) |
| Java | infinispan | TBD |

Yield depends on how tightly tests are coupled to the code they test. Repos with focused unit tests yield better than those with integration-heavy suites.

## Output format

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

## Files

| File | Purpose |
|------|---------|
| `collect_prs.py` | Step 1: Scrape PRs with test changes from GitHub |
| `validate_prs.py` | Step 2: Per-commit Docker validation (Go/Python/Java) |
| `auto_env.py` | Auto dependency resolution for Python repos |
| `generate_issues.py` | Step 3: LLM issue text + difficulty rating |
| `merge_dataset.py` | Step 4: Dedup and merge into dataset |
| `repos.json` | Repo list with language tags |
