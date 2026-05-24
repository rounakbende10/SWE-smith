# RH SWE-bench

SWE-bench style benchmark from Red Hat / Kubernetes ecosystem repos. Two approaches for generating instances:

1. **SWE-Smith synthetic bugs** — LLM rewrites, AST mutations, multi-file combinations validated against test suites
2. **Real PR pipeline** (`v3_pipeline/`) — Instances from actual merged bug-fix PRs, no SWE-Smith profiles needed

**Dataset**: [huggingface.co/datasets/rounakbende/rh-swe-bench](https://huggingface.co/datasets/rounakbende/rh-swe-bench)

## Current Dataset

502 verified instances across 25 repos (Go + Python).

| Repo | Count | | Repo | Count |
|------|-------|-|------|-------|
| kubernetes/kubectl | 124 | | osbuild/osbuild | 11 |
| ansible/molecule | 59 | | openshift/origin | 10 |
| etcd-io/etcd | 58 | | kubernetes/apimachinery | 9 |
| prometheus/prometheus | 39 | | kubevirt/kubevirt | 8 |
| containers/buildah | 31 | | containers/storage | 7 |
| operator-framework/operator-sdk | 26 | | containers/image | 5 |
| ansible/ansible | 24 | | openshift/router | 5 |
| containers/podman | 24 | | argoproj/argo-cd | 3 |
| kubernetes-sigs/kustomize | 23 | | openshift/oc | 3 |
| rook/rook | 14 | | containers/common | 2 |
| RedHatInsights/insights-core | 12 | | operator-framework/OLM | 2 |
| | | | openshift/installer | 1 |
| | | | tektoncd/pipeline | 1 |
| | | | openshift/CVO | 1 |

Difficulty: 65% easy (<15 min), 26% medium (15 min–1 hr), 8% hard (1–4 hrs), 1% very hard (>4 hrs)

## Prerequisites

```bash
pip install -e ".[all]"  # SWE-Smith (dependency for validation harness)
pip install docker unidiff requests openai
```

- Docker daemon running (local or remote via `DOCKER_HOST`)
- `GITHUB_TOKEN` for PR collection
- `OPENAI_API_KEY` for issue generation
- NERC OpenShift access (`oc login`) for running at scale

## Approach 1: SWE-Smith Synthetic Bugs

Uses SWE-Smith profiles to generate and validate bugs. Requires a registered profile for each repo.

### Generate bugs

```bash
# lm_rewrite (LLM reimplements functions with subtle bugs)
python3 -m swesmith.bug_gen.llm.rewrite <repo_profile> \
  --config_file configs/bug_gen/lm_rewrite.yml \
  --model <MODEL> --max_bugs 200 --n_workers 1

# Collect into patches JSON
python3 -m swesmith.bug_gen.collect_patches logs/bug_gen/<repo_profile>
```

### Validate on NERC

```bash
# Deploy DinD pod
./deploy_validation.sh deploy <repo_profile> <patches.json> <go|python|java>

# Monitor
./deploy_validation.sh status

# Collect results + generate issue text
./deploy_validation.sh collect <pod-name>
export OPENAI_API_KEY="sk-..."
./deploy_validation.sh issue-gen <pod-name>
```

### deploy_validation.sh commands

```
deploy <repo> <patches.json> <go|python|java>  Deploy validation pod
status [pod-name]                               Show status
collect <pod-name>                              Pull results locally
issue-gen <pod-name>                            Run issue generation
logs <pod-name>                                 Tail logs
cleanup <pod-name>                              Docker cleanup
delete <pod-name>                               Delete pod + PVC
```

### Typical yields

| Bug type | Valid rate | Notes |
|----------|-----------|-------|
| Procedural (AST mutations) | 5–25% | Mostly easy difficulty |
| lm_rewrite | 2–25% | Best yields: insights-core (24%), buildah (6%) |
| Combined (multi-file) | 60–100% | Combines 2–5 validated patches |

## Approach 2: Real PR Pipeline

Creates instances from actual merged PRs. No SWE-Smith profiles needed — works for any repo with a test suite. See [`v3_pipeline/README.md`](v3_pipeline/README.md) for detailed usage.

```bash
cd redhat-bench/v3_pipeline

# 1. Collect PRs with test changes
export GITHUB_TOKEN=$(gh auth token)
python collect_prs.py repos.json collected.jsonl

# 2. Validate
python validate_prs.py collected.jsonl --lang go --output-dir ./results

# 3. Generate issue text + difficulty
export OPENAI_API_KEY="sk-..."
python generate_issues.py ./results --output rated.json

# 4. Merge into dataset
python merge_dataset.py rated.json --dataset path/to/dataset.json
```

### Typical yields

| Repos | Yield | Why |
|-------|-------|-----|
| prometheus, kustomize, rook, router | 20–45% | Tightly coupled unit tests |
| etcd, buildah, podman | 0–5% | Integration-heavy test suites |
| cri-o, oc, OLM | 0% | Tests don't catch bugs at unit level |

## Language-Specific Notes

### Go repos

Simplest setup. Use `golang:1.22` base image, `go test -v -short -count=1`. The v3 pipeline dynamically adds test packages from the patch diff to the test command.

### Python repos

SWE-Smith approach needs a **sweenv conda spec** (see below). The v3 pipeline uses `python:3.12-bookworm` with `pip install` instead — no conda needed.

<details>
<summary>Generating a sweenv for SWE-Smith profiles</summary>

```bash
conda config --set anaconda_tos_accepted True
conda create -n sweenv python=3.12 -c conda-forge --override-channels -y
conda run -n sweenv bash -c 'cd /tmp/repo && pip install -e ".[test]"'
conda env export -n sweenv --override-channels -c conda-forge > sweenv.yml
sed -i 's/^name: .*/name: testbed/' sweenv.yml
sed -i '/^prefix:/d' sweenv.yml
```

| Issue | Fix |
|-------|-----|
| `SpecNotFound: is empty` | `conda config --set anaconda_tos_accepted True` |
| Wrong channel | Use `--override-channels -c conda-forge` |
| Env name mismatch | `sed -i 's/^name: .*/name: testbed/' sweenv.yml` |
</details>

### Java repos

Uses `maven:3.9.6-eclipse-temurin-17` base. Maven test suites can take 20–30 min per PR. Set `--timeout 1800` for validation.

## Verification

Optional quality filter using LLM grading:

```bash
python3 verify_dataset.py dataset.json --output verified.json --model <MODEL> --workers 8
```

## Harbor Adapter

Convert verified instances to Harbor format for the coding agent leaderboard:

```bash
PYTHONPATH=harbor-adapter/src python3 -m rh_swebench_adapter.main \
  --dataset dataset.json --task-dir datasets/rh-swe-bench
```

## Known SWE-Smith Issues

| Issue | Description | Workaround |
|-------|-------------|------------|
| `gather.py` PASS_TO_FAIL bug | Checks wrong field in validation reports | Use `deploy_validation.sh collect` instead |
| Empty mirror repos | `rounakbende10/<repo>` repos are empty | Script auto-falls back to upstream |
| `pull_image()` Docker Hub | Tries Docker Hub before local images | Monkey-patched in deploy script |
| Docker overload | `ReadTimeout` with >2 workers | Default to 2 workers |
| DinD pods die after ~24h | Docker daemon exits (Completed) | Recreate pod, results survive on PVC |

## Directory Structure

```
redhat-bench/
  README.md                 # This file
  deploy_validation.sh      # NERC pod deployment + management
  verify_dataset.py         # LLM-based quality verification
  harbor-adapter/           # Harbor format converter
  v3_pipeline/              # Real PR validation pipeline
    README.md
    collect_prs.py          # Step 1: Scrape PRs from GitHub
    validate_prs.py         # Step 2: Per-commit Docker validation
    generate_issues.py      # Step 3: Issue text + difficulty rating
    merge_dataset.py        # Step 4: Merge into dataset
    repos.json      # Example repo list
```
