#!/bin/bash
# Deploy and manage NERC validation pods for any language.
# Incorporates all fixes: upstream repo fallback, Docker cleanup, 2 workers, no crash-on-failure.
#
# Usage:
#   ./deploy_validation.sh deploy <repo_profile> <patches.json> <go|python|java> [--name pod-name] [--workers 2]
#   ./deploy_validation.sh status [pod-name]
#   ./deploy_validation.sh collect <pod-name>         # Pull results locally
#   ./deploy_validation.sh issue-gen <pod-name>       # Run issue generation on completed validation
#   ./deploy_validation.sh logs <pod-name>            # Tail validation logs
#   ./deploy_validation.sh cleanup <pod-name>         # Docker cleanup on pod
#   ./deploy_validation.sh delete <pod-name>          # Delete pod + PVC
#
# Examples:
#   ./deploy_validation.sh deploy etcd-io__etcd.ec166e22 logs/bug_gen/etcd_lm_rewrite.json go
#   ./deploy_validation.sh deploy quarkusio__quarkus.99a220ef logs/bug_gen/quarkus_lm_rewrite.json java
#   ./deploy_validation.sh deploy ansible__ansible.a5b61bc6 logs/bug_gen/ansible_lm_rewrite.json python
#   ./deploy_validation.sh status
#   ./deploy_validation.sh collect validate-etcd

set -uo pipefail

NAMESPACE="rh-swe-bench"
SWESMITH_REPO="${SWESMITH_REPO:-https://github.com/rounakbende10/SWE-smith.git}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Helpers ───

die() { echo "ERROR: $*" >&2; exit 1; }

base_image_for_lang() {
    case "$1" in
        python) echo "python:3.13-bookworm" ;;
        java)   echo "python:3.13-slim" ;;
        go)     echo "golang:1.26" ;;
        *)      die "Unknown language: $1" ;;
    esac
}

pod_name_from_repo() {
    local repo=$1
    local suffix=${2:-}
    local short=$(echo "$repo" | sed 's/.*__//' | cut -d'.' -f1 | head -c 15)
    if [ -n "$suffix" ]; then
        echo "validate-${short}-${suffix}"
    else
        echo "validate-${short}"
    fi
}

# ─── Deploy ───

cmd_deploy() {
    local REPO=${1:?Usage: deploy <repo_profile> <patches.json> <go|python|java>}
    local PATCHES=${2:?Usage: deploy <repo_profile> <patches.json> <go|python|java>}
    local LANGUAGE=${3:?Usage: deploy <repo_profile> <patches.json> <go|python|java>}
    shift 3

    local POD_NAME="" WORKERS=2
    while [ $# -gt 0 ]; do
        case "$1" in
            --name)    POD_NAME="$2"; shift 2 ;;
            --workers) WORKERS="$2"; shift 2 ;;
            *)         die "Unknown flag: $1" ;;
        esac
    done

    [ -z "$POD_NAME" ] && POD_NAME=$(pod_name_from_repo "$REPO")
    local PVC="${POD_NAME}-pvc"
    local IMAGE=$(base_image_for_lang "$LANGUAGE")

    [ ! -f "$PATCHES" ] && die "Patches file not found: $PATCHES"
    local PATCH_COUNT=$(python3 -c "import json; print(len(json.load(open('$PATCHES'))))")
    echo "Repo:     $REPO"
    echo "Language: $LANGUAGE"
    echo "Patches:  $PATCHES ($PATCH_COUNT patches)"
    echo "Pod:      $POD_NAME"
    echo "Workers:  $WORKERS"
    echo "Image:    $IMAGE"
    echo ""

    if oc get pod "$POD_NAME" -n "$NAMESPACE" > /dev/null 2>&1; then
        die "Pod $POD_NAME already exists. Delete with: $0 delete $POD_NAME"
    fi

    # Create pod YAML
    cat > /tmp/${POD_NAME}.yaml << YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
  namespace: $NAMESPACE
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 50Gi
  storageClassName: ocs-external-storagecluster-ceph-rbd
---
apiVersion: v1
kind: Pod
metadata:
  name: $POD_NAME
  namespace: $NAMESPACE
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 0
    runAsGroup: 0
    fsGroup: 0
  containers:
    - name: dind
      image: docker:27-dind
      securityContext:
        privileged: true
      volumeMounts:
        - {name: workspace, mountPath: /workspace}
        - {name: docker-storage, mountPath: /var/lib/docker}
      env:
        - {name: DOCKER_TLS_CERTDIR, value: ""}
        - {name: DOCKER_BUILDKIT, value: "1"}
    - name: runner
      image: $IMAGE
      securityContext:
        runAsUser: 0
        runAsGroup: 0
        allowPrivilegeEscalation: true
      command: ["/bin/bash", "-c", "sleep infinity"]
      volumeMounts:
        - {name: workspace, mountPath: /workspace}
      env:
        - {name: DOCKER_HOST, value: "tcp://localhost:2375"}
        - {name: DOCKER_BUILDKIT, value: "1"}
      resources:
        requests: {cpu: "4", memory: 8Gi}
        limits: {cpu: "8", memory: 16Gi}
  volumes:
    - name: workspace
      persistentVolumeClaim:
        claimName: $PVC
    - name: docker-storage
      emptyDir:
        sizeLimit: 40Gi
YAML

    echo "Creating pod..."
    oc apply -f /tmp/${POD_NAME}.yaml 2>&1
    echo "Waiting for pod to be ready..."
    oc wait pod/$POD_NAME --for=condition=Ready --timeout=300s -n "$NAMESPACE" 2>/dev/null || true
    sleep 10

    local pod_status=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
    [ "$pod_status" != "Running" ] && die "Pod not running: $pod_status"

    # Copy patches
    echo "Copying patches..."
    oc cp "$PATCHES" "$POD_NAME:/workspace/patches.json" -c runner -n "$NAMESPACE" 2>/dev/null

    # Generate and copy setup script
    _generate_setup_script "$REPO" "$LANGUAGE" "$WORKERS" > /tmp/${POD_NAME}-setup.sh
    oc cp /tmp/${POD_NAME}-setup.sh "$POD_NAME:/workspace/setup.sh" -c runner -n "$NAMESPACE" 2>/dev/null

    echo "Running setup (install deps, clone SWE-Smith, build image, start validation)..."
    echo "This runs in background. Monitor with: $0 logs $POD_NAME"
    oc exec "$POD_NAME" -c runner -n "$NAMESPACE" -- bash -c "
        nohup bash /workspace/setup.sh > /workspace/setup.log 2>&1 &
        echo \$!
    " 2>/dev/null
    echo ""
    echo "Pod $POD_NAME deployed and setup started."
    echo "  Logs:    $0 logs $POD_NAME"
    echo "  Status:  $0 status $POD_NAME"
    echo "  Collect: $0 collect $POD_NAME"
}

_generate_setup_script() {
    local REPO=$1 LANGUAGE=$2 WORKERS=$3

    # Extract owner/name from profile name (e.g. etcd-io__etcd.ec166e22 -> etcd-io/etcd)
    local OWNER=$(echo "$REPO" | cut -d'_' -f1)
    local NAME=$(echo "$REPO" | sed 's/^[^_]*__//' | cut -d'.' -f1)
    local COMMIT=$(echo "$REPO" | rev | cut -d'.' -f1 | rev)

    cat << 'SETUP_HEADER'
#!/bin/bash
set -uo pipefail
export DOCKER_HOST=tcp://localhost:2375

echo "=== Setup started at $(date) ==="

SETUP_HEADER

    # Language-specific deps
    case "$LANGUAGE" in
        go) cat << 'DEPS'
echo "Installing system deps..."
apt-get update -qq && apt-get install -y -qq \
    docker.io git python3 python3-pip gcc make pkg-config \
    libgpgme-dev libassuan-dev libgpg-error-dev libseccomp-dev \
    libdevmapper-dev libglib2.0-dev libbtrfs-dev libsystemd-dev \
    libselinux1-dev libapparmor-dev libudev-dev libkrb5-dev \
    > /dev/null 2>&1
DEPS
        ;;
        java) cat << 'DEPS'
echo "Installing system deps..."
apt-get update -qq && apt-get install -y -qq docker.io git > /dev/null 2>&1
DEPS
        ;;
        python) cat << 'DEPS'
echo "Installing system deps..."
apt-get update -qq && apt-get install -y -qq docker.io git > /dev/null 2>&1
DEPS
        ;;
    esac

    cat << SETUP_BODY

# Wait for Docker
echo "Waiting for Docker..."
for i in \$(seq 1 60); do docker info > /dev/null 2>&1 && break; sleep 3; done
docker info > /dev/null 2>&1 || { echo "ERROR: Docker not ready"; exit 1; }
echo "Docker ready"

# Clone and install SWE-Smith
cd /workspace
if [ ! -d "SWE-smith" ]; then
    echo "Cloning SWE-Smith..."
    git clone ${SWESMITH_REPO} SWE-smith
fi
cd SWE-smith
pip install --break-system-packages -e ".[all]" 2>&1 | tail -3
echo "SWE-Smith installed"

# Patch pull_image to check local images before trying Docker Hub
python3 -c "
import site, os
site_dir = site.getsitepackages()[0]
with open(os.path.join(site_dir, 'swesmith_patch.py'), 'w') as f:
    f.write('''
import swesmith.profiles.base as base
import docker
_orig_pull = base.RepoProfile.pull_image
def pull_image_fixed(self):
    try:
        client = docker.from_env()
        client.images.get(self.image_name)
        return
    except: pass
    try: _orig_pull(self)
    except:
        try:
            client = docker.from_env()
            client.images.get(self.image_name)
        except:
            raise RuntimeError(f\"Image {self.image_name} not found locally or on Docker Hub\")
base.RepoProfile.pull_image = pull_image_fixed
''')
customize = os.path.join(site_dir, 'usercustomize.py')
existing = open(customize).read() if os.path.exists(customize) else ''
if 'swesmith_patch' not in existing:
    with open(customize, 'a') as f:
        f.write('\\\nimport swesmith_patch\\\n')
"
echo "pull_image patched"

export SSL_CERT_FILE=\$(python3 -c "import certifi; print(certifi.where())")
export REQUESTS_CA_BUNDLE=\$SSL_CERT_FILE

# Copy patches
mkdir -p logs/bug_gen
cp /workspace/patches.json logs/bug_gen/${REPO}_patches.json

# Build Docker image
echo "Building Docker image for ${REPO}..."
python3 << 'PYEOF'
import subprocess, sys
from swesmith.profiles import registry

repo = "${REPO}"
rp = registry.get(repo)

# Try building with the profile's build_image (uses mirror repo)
try:
    rp.build_image()
    print(f"Built image: {rp.image_name}")
    sys.exit(0)
except Exception as e:
    print(f"Profile build failed: {e}")
    print("Falling back to upstream repo build...")

# Fallback: build from upstream repo
owner = "${OWNER}"
name = "${NAME}"
commit = "${COMMIT}"
upstream_url = f"https://github.com/{owner}/{name}"

dockerfile = rp.dockerfile
# Replace mirror URL with upstream URL in dockerfile
mirror_name = rp.mirror_name
dockerfile = dockerfile.replace(f"https://github.com/{mirror_name}", upstream_url)
# Also try without .git
dockerfile = dockerfile.replace(mirror_name, f"{owner}/{name}")

# If the dockerfile doesn't have a checkout for the commit, add one
if commit not in dockerfile and "git checkout" not in dockerfile:
    dockerfile = dockerfile.replace(
        "WORKDIR /testbed",
        f"WORKDIR /testbed\nRUN git checkout {commit}"
    )

df_path = f"logs/build_images/env/{repo}/Dockerfile_upstream"
import os; os.makedirs(os.path.dirname(df_path), exist_ok=True)
with open(df_path, "w") as f:
    f.write(dockerfile)

print(f"Upstream Dockerfile written to {df_path}")
print(dockerfile[:500])

result = subprocess.run(
    f"docker build -f {df_path} --platform linux/x86_64 --no-cache -t {rp.image_name} .",
    shell=True, capture_output=True, text=True
)
if result.returncode == 0:
    print(f"Built image: {rp.image_name}")
else:
    print(f"Build failed: {result.stderr[-500:]}")
    sys.exit(1)
PYEOF

if [ \$? -ne 0 ]; then
    echo "ERROR: Docker image build failed"
    exit 1
fi

echo "Docker images:"
docker images | grep swesmith

# Start periodic Docker cleanup in background (every 10 min)
(while true; do sleep 600; docker container prune -f > /dev/null 2>&1; docker builder prune -f > /dev/null 2>&1; done) &
CLEANUP_PID=\$!

# Run validation
echo ""
echo "=== Starting validation at \$(date) ==="
echo "Workers: ${WORKERS}"

python3 -m swesmith.harness.valid logs/bug_gen/${REPO}_patches.json --workers ${WORKERS} 2>&1 | tee /workspace/validate.log

# Stop cleanup and do final prune
kill \$CLEANUP_PID 2>/dev/null
docker container prune -f > /dev/null 2>&1
docker builder prune -f > /dev/null 2>&1

echo ""
echo "=== Validation complete at \$(date) ==="

# Print summary
python3 << 'SUMMARY'
import json, os
base = "logs/run_validation"
for repo_dir in sorted(os.listdir(base)):
    path = os.path.join(base, repo_dir)
    if not os.path.isdir(path): continue
    total = valid = timed = no_f2p = 0
    for inst in os.listdir(path):
        rpt = os.path.join(path, inst, "report.json")
        if os.path.exists(rpt):
            d = json.load(open(rpt))
            total += 1
            if "timed_out" in d: timed += 1
            elif isinstance(d.get("FAIL_TO_PASS", []), list) and len(d.get("FAIL_TO_PASS", [])) > 0: valid += 1
            else: no_f2p += 1
    if total > 0:
        print(f"{repo_dir}: {valid} valid / {total} done ({timed} timed out)")
SUMMARY
SETUP_BODY
}

# ─── Status ───

cmd_status() {
    local TARGET=${1:-}

    echo "========================================"
    echo "  NERC Validation Status — $(date '+%Y-%m-%d %H:%M')"
    echo "========================================"
    echo ""

    local pods
    if [ -n "$TARGET" ]; then
        pods="$TARGET"
    else
        pods=$(oc get pods -n "$NAMESPACE" --no-headers 2>/dev/null | awk '{print $1}')
    fi

    for pod in $pods; do
        local phase=$(oc get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
        local age=$(oc get pod "$pod" -n "$NAMESPACE" --no-headers 2>/dev/null | awk '{print $5}')
        echo "  $pod [$phase] ($age)"

        [ "$phase" != "Running" ] && continue

        oc exec "$pod" -c runner -n "$NAMESPACE" -- python3 -c "
import json, os
base = '/workspace/SWE-smith/logs/run_validation'
if not os.path.exists(base):
    print('    (no validation data)')
else:
    for repo in sorted(os.listdir(base)):
        repo_dir = os.path.join(base, repo)
        if not os.path.isdir(repo_dir): continue
        total = valid = timed = no_f2p = docker_fail = 0
        by_type = {}
        for inst in os.listdir(repo_dir):
            rpt = os.path.join(repo_dir, inst, 'report.json')
            if not os.path.exists(rpt): continue
            d = json.load(open(rpt))
            total += 1
            bug_type = 'lm_rw' if 'lm_rewrite' in inst else 'lm_mod' if 'lm_modify' in inst else 'proc'
            by_type.setdefault(bug_type, {'v': 0, 't': 0})
            by_type[bug_type]['t'] += 1
            if d.get('missing_pregold_output'): docker_fail += 1
            elif 'timed_out' in d: timed += 1
            elif isinstance(d.get('FAIL_TO_PASS', []), list) and len(d.get('FAIL_TO_PASS', [])) > 0:
                valid += 1
                by_type[bug_type]['v'] += 1
            else: no_f2p += 1
        if total > 0:
            parts = [f'{valid} valid', f'{total} done']
            if timed: parts.append(f'{timed} timeout')
            if docker_fail: parts.append(f'{docker_fail} docker-err')
            detail = ' | '.join(f'{k}:{v[\"v\"]}/{v[\"t\"]}' for k, v in by_type.items())
            print(f'    {repo}: {\" | \".join(parts)} [{detail}]')
running = os.popen('pgrep -f swesmith.harness 2>/dev/null').read().strip()
print(f'    harness: {\"running\" if running else \"idle\"} ')
" 2>/dev/null || echo "    (cannot exec)"
    done
    echo ""
}

# ─── Collect ───

cmd_collect() {
    local POD=${1:?Usage: collect <pod-name>}

    echo "Collecting results from $POD..."
    local LOCAL_DIR="$PROJECT_ROOT/logs/task_insts"
    mkdir -p "$LOCAL_DIR"

    # Build task_insts on the pod, then pull
    oc exec "$POD" -c runner -n "$NAMESPACE" -- python3 -c "
import json, os
from swesmith.profiles import registry

base = '/workspace/SWE-smith/logs/run_validation'
os.makedirs('/workspace/SWE-smith/logs/task_insts', exist_ok=True)

for repo_dir_name in sorted(os.listdir(base)):
    repo_path = os.path.join(base, repo_dir_name)
    if not os.path.isdir(repo_path): continue

    try:
        rp = registry.get(repo_dir_name)
        mirror_name = rp.mirror_name
        image_name = rp.image_name
    except:
        mirror_name = f'rounakbende10/{repo_dir_name}'
        image_name = f'swebench/swesmith.{repo_dir_name}'

    instances = []
    for inst_id in sorted(os.listdir(repo_path)):
        rpt = os.path.join(repo_path, inst_id, 'report.json')
        patch_path = os.path.join(repo_path, inst_id, 'patch.diff')
        if not os.path.exists(rpt) or not os.path.exists(patch_path): continue
        d = json.load(open(rpt))
        f2p = d.get('FAIL_TO_PASS', [])
        p2p = d.get('PASS_TO_PASS', [])
        if not isinstance(f2p, list) or len(f2p) == 0: continue
        if not isinstance(p2p, list) or len(p2p) == 0: continue
        with open(patch_path) as f:
            patch = f.read()
        instances.append({
            'instance_id': inst_id, 'repo': mirror_name, 'patch': patch,
            'FAIL_TO_PASS': f2p, 'PASS_TO_PASS': p2p, 'image_name': image_name,
        })

    if instances:
        out = f'/workspace/SWE-smith/logs/task_insts/{repo_dir_name}.json'
        with open(out, 'w') as f:
            json.dump(instances, f, indent=4)
        print(f'{repo_dir_name}: {len(instances)} valid instances -> {out}')
" 2>/dev/null

    # Pull the task_insts files
    for f in $(oc exec "$POD" -c runner -n "$NAMESPACE" -- ls /workspace/SWE-smith/logs/task_insts/ 2>/dev/null | grep -v "__ig_"); do
        echo "  Pulling $f..."
        oc cp "$POD:/workspace/SWE-smith/logs/task_insts/$f" "$LOCAL_DIR/${POD}__${f}" -c runner -n "$NAMESPACE" 2>/dev/null
    done

    # Also pull ig_llm files if they exist
    for f in $(oc exec "$POD" -c runner -n "$NAMESPACE" -- ls /workspace/SWE-smith/logs/task_insts/ 2>/dev/null | grep "__ig_llm"); do
        echo "  Pulling $f..."
        oc cp "$POD:/workspace/SWE-smith/logs/task_insts/$f" "$LOCAL_DIR/${POD}__${f}" -c runner -n "$NAMESPACE" 2>/dev/null
    done

    echo "Results saved to $LOCAL_DIR/"
}

# ─── Issue Gen ───

cmd_issue_gen() {
    local POD=${1:?Usage: issue-gen <pod-name>}
    local OPENAI_KEY=${OPENAI_API_KEY:?Set OPENAI_API_KEY env var}

    echo "Running issue generation on $POD..."

    # First collect/create task_insts
    oc exec "$POD" -c runner -n "$NAMESPACE" -- python3 -c "
import json, os
from swesmith.profiles import registry
base = '/workspace/SWE-smith/logs/run_validation'
os.makedirs('/workspace/SWE-smith/logs/task_insts', exist_ok=True)
for repo_dir_name in sorted(os.listdir(base)):
    repo_path = os.path.join(base, repo_dir_name)
    if not os.path.isdir(repo_path): continue
    try:
        rp = registry.get(repo_dir_name)
        mirror_name = rp.mirror_name
        image_name = rp.image_name
    except: continue
    instances = []
    for inst_id in sorted(os.listdir(repo_path)):
        rpt = os.path.join(repo_path, inst_id, 'report.json')
        patch_path = os.path.join(repo_path, inst_id, 'patch.diff')
        if not os.path.exists(rpt) or not os.path.exists(patch_path): continue
        d = json.load(open(rpt))
        f2p = d.get('FAIL_TO_PASS', [])
        p2p = d.get('PASS_TO_PASS', [])
        if not isinstance(f2p, list) or len(f2p) == 0: continue
        if not isinstance(p2p, list) or len(p2p) == 0: continue
        with open(patch_path) as f: patch = f.read()
        instances.append({
            'instance_id': inst_id, 'repo': mirror_name, 'patch': patch,
            'FAIL_TO_PASS': f2p, 'PASS_TO_PASS': p2p, 'image_name': image_name,
        })
    if instances:
        out = f'/workspace/SWE-smith/logs/task_insts/{repo_dir_name}.json'
        with open(out, 'w') as f: json.dump(instances, f, indent=4)
        print(f'{repo_dir_name}: {len(instances)} valid instances')
" 2>/dev/null

    # Create issue_gen config
    oc exec "$POD" -c runner -n "$NAMESPACE" -- bash -c "
cat > /workspace/SWE-smith/configs/issue_gen/ig_rh.yaml << 'YAML'
model: openai/gpt-5-mini
settings:
  n_instructions: 1
  max_var_tokens: 10000
system: |-
  You are a software engineer helping to create a realistic dataset of synthetic GitHub issues.
  You will be given a patch that introduces a bug, test output, and test source code.
  Output: A realistic GitHub issue. DO NOT explain the fix, do not mention tests.
demonstration: null
instance: |-
  Write a GitHub issue for the following patch.
  <IMPORTANT>
  - DO NOT GIVE AWAY THE FIX!
  - DO NOT SAY THAT EXISTING TEST(s) FAILED.
  - DO NOT SUGGEST RUNNING ANY TESTING COMMANDS.
  </IMPORTANT>
  <patch>
  {{patch}}
  </patch>
  <test_output>
  {{test_output}}
  </test_output>
  <test_source_code>
  {% for test in test_funcs[:5] %}
  {{test}}
  {% endfor %}
  </test_source_code>
  **Issue Text**
  <START WRITING>
YAML
" 2>/dev/null

    # Run issue_gen for each task_insts file
    oc exec "$POD" -c runner -n "$NAMESPACE" -- bash -c "
cd /workspace/SWE-smith
export OPENAI_API_KEY='$OPENAI_KEY'
for f in logs/task_insts/*.json; do
    case \"\$f\" in *__ig_*) continue ;; esac
    echo \"=== \$f ===\"
    python3 -m swesmith.issue_gen.generate -d \"\$f\" -c configs/issue_gen/ig_rh.yaml -w 4 2>&1 | grep -E 'Will create|100%|Wrote|ERROR'
done
" 2>&1
}

# ─── Logs ───

cmd_logs() {
    local POD=${1:?Usage: logs <pod-name>}
    echo "=== Setup log ==="
    oc exec "$POD" -c runner -n "$NAMESPACE" -- tail -30 /workspace/setup.log 2>/dev/null
    echo ""
    echo "=== Validation log ==="
    oc exec "$POD" -c runner -n "$NAMESPACE" -- tail -20 /workspace/validate.log 2>/dev/null
}

# ─── Cleanup ───

cmd_cleanup() {
    local POD=${1:?Usage: cleanup <pod-name>}
    echo "Cleaning Docker on $POD..."
    oc exec "$POD" -c runner -n "$NAMESPACE" -- bash -c "
        export DOCKER_HOST=tcp://localhost:2375
        docker kill \$(docker ps -q) 2>/dev/null
        docker container prune -f 2>/dev/null
        docker builder prune -f 2>/dev/null
        echo 'Done'
    " 2>/dev/null
}

# ─── Delete ───

cmd_delete() {
    local POD=${1:?Usage: delete <pod-name>}
    local PVC="${POD}-pvc"
    echo "Deleting pod $POD and PVC $PVC..."
    oc delete pod "$POD" -n "$NAMESPACE" --grace-period=0 2>/dev/null
    oc delete pvc "$PVC" -n "$NAMESPACE" 2>/dev/null
    echo "Deleted"
}

# ─── Main ───

CMD=${1:-help}
shift || true

case "$CMD" in
    deploy)    cmd_deploy "$@" ;;
    status)    cmd_status "$@" ;;
    collect)   cmd_collect "$@" ;;
    issue-gen) cmd_issue_gen "$@" ;;
    logs)      cmd_logs "$@" ;;
    cleanup)   cmd_cleanup "$@" ;;
    delete)    cmd_delete "$@" ;;
    *)
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  deploy <repo> <patches.json> <go|python|java>  Deploy validation pod"
        echo "  status [pod-name]                               Show status of all/one pod"
        echo "  collect <pod-name>                              Pull results locally"
        echo "  issue-gen <pod-name>                            Run issue generation"
        echo "  logs <pod-name>                                 Tail logs"
        echo "  cleanup <pod-name>                              Docker cleanup"
        echo "  delete <pod-name>                               Delete pod + PVC"
        echo ""
        echo "Examples:"
        echo "  $0 deploy etcd-io__etcd.ec166e22 logs/bug_gen/etcd_lm_rewrite.json go"
        echo "  $0 deploy ansible__ansible.a5b61bc6 logs/bug_gen/ansible_lm_rewrite.json python"
        echo "  $0 deploy quarkusio__quarkus.99a220ef logs/bug_gen/quarkus_lm_rewrite.json java"
        echo "  $0 status"
        echo "  $0 collect validate-etcd"
        exit 1
        ;;
esac
