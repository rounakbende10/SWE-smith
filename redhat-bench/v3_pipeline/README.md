# v3 PR Validation Pipeline

Creates SWE-bench instances from real merged pull requests. Works for Go, Python, and Java repos — no SWE-Smith profiles needed.

## How it works

1. **Collect** merged PRs that change both code and test files
2. **Validate** each PR by building a Docker image at the PR's base commit, applying the test changes, running tests (should fail), applying the fix, running tests again (should pass)
3. **Generate** issue text and difficulty ratings using an LLM
4. **Merge** valid instances into the dataset

## Prerequisites

```bash
pip install swesmith docker unidiff requests openai
```

- Docker daemon running (local Docker, Podman, or remote via `DOCKER_HOST`)
- `GITHUB_TOKEN` for PR collection (`export GITHUB_TOKEN=$(gh auth token)`)
- `OPENAI_API_KEY` for issue generation

## Quick start

```bash
cd redhat-bench/v3_pipeline

# 1. Collect PRs (edit repos.json or create your own)
export GITHUB_TOKEN=$(gh auth token)
python collect_prs.py repos.json collected_prs.jsonl --max-pulls 200

# 2. Validate (pick your language)
python validate_prs.py collected_prs.jsonl --lang go --output-dir ./results --timeout 600

# 3. Generate issue text + difficulty rating
export OPENAI_API_KEY=your-key
python generate_issues.py ./results --output instances_rated.json

# 4. Merge into dataset
python merge_dataset.py instances_rated.json --dataset ../path/to/dataset.json

# Use --dry-run to preview without writing:
python merge_dataset.py instances_rated.json --dataset ../path/to/dataset.json --dry-run
```

## Running at scale on OpenShift / NERC

For large batches, use `deploy_validation.sh` (one level up) to create Docker-in-Docker pods on an OpenShift cluster, then copy the scripts and batch files to the pods:

```bash
# Upload script + data to a pod
oc cp validate_prs.py <namespace>/<pod>:/tmp/validate_prs.py -c runner
oc cp collected_prs.jsonl <namespace>/<pod>:/tmp/batch.jsonl -c runner

# Start validation in background
oc exec <pod> -c runner -- bash -c '
  export DOCKER_HOST=tcp://localhost:2375
  nohup python3 -u /tmp/validate_prs.py /tmp/batch.jsonl --lang go \
    --output-dir /workspace/SWE-smith/logs/v3_validation > /tmp/v3_run.log 2>&1 &
'

# Monitor
oc exec <pod> -c runner -- tail -f /tmp/v3_run.log
oc exec <pod> -c runner -- grep -c "\[VALID\]" /tmp/v3_run.log
```

## Repo input format

```json
[
    {"repo": "owner/name", "lang": "go"},
    {"repo": "owner/name", "lang": "python"},
    {"repo": "owner/name", "lang": "java"}
]
```

## Expected yields

Based on our runs:

| Language | Repos | Yield |
|----------|-------|-------|
| Go | prometheus, kustomize, rook, router | 20-45% |
| Go | etcd, buildah, podman, cri-o | 0-5% |
| Python | TBD | TBD |
| Java | TBD | TBD |

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
