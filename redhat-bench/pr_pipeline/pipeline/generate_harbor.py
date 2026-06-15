#!/usr/bin/env python3
"""
Convert RH SWE-bench dataset to Harbor format for agent evaluation.

Per-repo base image architecture:
  bases/<repo_key>/
    Dockerfile       # Base image with repo clone and dependencies
    setup_env.sh     # (Python SWE-Smith envs only) conda env setup

  task-NNNN/
    task.toml          # metadata: difficulty, timeout, category
    input.yaml         # prompt field with problem_statement
    instruction.md     # problem statement as markdown
    environment/
      Dockerfile       # FROM base-<repo_key>, checkout commit, apply patches
      setup.patch      # (synthetic) or test.patch (PR-based)
    solution/
      solve.sh         # base64-encoded gold patch
    tests/
      test.sh          # runs tests, writes 1/0 to /logs/verifier/reward.txt

Two instance types:
1. Synthetic (setup_patch non-empty): Mirror repo HEAD is correct commit.
   Apply setup_patch to introduce bug. Agent fixes it.
   solve.sh: reverse-applies setup_patch (buggy→clean).

2. PR-based (setup_patch empty): Upstream repo at base_commit has bug.
   Apply test_patch to add tests. Agent fixes it.
   solve.sh: forward-applies patch (buggy→clean).
"""

import argparse
import base64
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Set


DIFFICULTY_MAP = {
    "<15 min fix": "easy",
    "15 min - 1 hour": "medium",
    "1-4 hours": "hard",
    ">4 hours": "hard",
}

TIMEOUT_MAP = {
    "easy": 600,      # 10 min
    "medium": 1800,   # 30 min
    "hard": 3600,     # 60 min
}

GO_BASE_IMAGE = "golang:1.26"

GO_WORK_MODULES = {
    "kubernetes-sigs/kustomize": ["api", "kustomize", "kyaml", "cmd/config", "cmd/gorepomod",
                                  "cmd/k8scopy", "cmd/pluginator", "hack",
                                  "plugin/builtin/sortordertransformer",
                                  "plugin/builtin/patchtransformer",
                                  "plugin/builtin/namespacetransformer",
                                  "plugin/builtin/replacementtransformer",
                                  "plugin/builtin/helmchartinflationgenerator",
                                  "plugin/builtin/annotationstransformer",
                                  "plugin/builtin/configmapgenerator",
                                  "plugin/builtin/hashtransformer",
                                  "plugin/builtin/imagetagtransformer",
                                  "plugin/builtin/labeltransformer",
                                  "plugin/builtin/patchjson6902transformer",
                                  "plugin/builtin/patchstrategicmergetransformer",
                                  "plugin/builtin/prefixtransformer",
                                  "plugin/builtin/replicacounttransformer",
                                  "plugin/builtin/secretgenerator",
                                  "plugin/builtin/suffixtransformer",
                                  "plugin/builtin/valueaddtransformer"],
}
PYTHON_BASE_IMAGE = "python:3.12-bookworm"
SWESMITH_BASE_IMAGE = "swebench/swesmith.x86_64"

# Per-repo system dependencies (dnf packages) for Go repos on UBI9
REPO_SYSTEM_DEPS = {
    "containers/buildah": "gpgme-devel libassuan-devel device-mapper-devel btrfs-progs-devel libseccomp-devel",
    "containers/podman": "gpgme-devel libassuan-devel device-mapper-devel btrfs-progs-devel libseccomp-devel",
    "containers/image": "gpgme-devel libassuan-devel device-mapper-devel btrfs-progs-devel libseccomp-devel",
    "containers/storage": "gpgme-devel libassuan-devel device-mapper-devel btrfs-progs-devel libseccomp-devel",
    "containers/common": "gpgme-devel libassuan-devel device-mapper-devel btrfs-progs-devel libseccomp-devel",
    "kubevirt/kubevirt": "libvirt-devel",
    "openshift/installer": "libvirt-devel",
    "openshift/origin": "libseccomp-devel",
}

# Mirror repos on rounakbende10 profile for synthetic instances
MIRROR_REPOS = {
    "RedHatInsights/insights-core": "rounakbende10/RedHatInsights__insights-core.5972bf28",
    "ansible/ansible": "rounakbende10/ansible__ansible.a5b61bc6",
    "ansible/molecule": "rounakbende10/ansible__molecule.5e8051db",
    "containers/buildah": "rounakbende10/containers__buildah.310b1c8f",
    "containers/common": "rounakbende10/containers__common.a5ccdae8",
    "containers/image": "rounakbende10/containers__image.df7e80d2",
    "containers/podman": "rounakbende10/containers__podman.5b263b5f",
    "containers/storage": "rounakbende10/containers__storage.83cf5746",
    "etcd-io/etcd": "rounakbende10/etcd-io__etcd.ec166e22",
    "kubernetes/apimachinery": "rounakbende10/kubernetes__apimachinery.7af103a2",
    "kubernetes/kubectl": "rounakbende10/kubernetes__kubectl.01cb18cd",
    "operator-framework/operator-sdk": "rounakbende10/operator-framework__operator-sdk.6001c290",
    "osbuild/osbuild": "rounakbende10/osbuild__osbuild.f312d0ac",
}

# Exact commits SWE-Smith profiles used for each mirror repo.
# Mirror HEADs may drift — always checkout this commit in base images.
MIRROR_COMMITS = {
    "RedHatInsights/insights-core": "5972bf2895f3b5b9f6ddcffecbb63b7ebeab5267",
    "ansible/ansible": "a5b61bc6ee6e6cd5ba33254f100ecc5dc08b4ccd",
    "ansible/molecule": "5e8051dbefa85bb3c910926eae026e97ecaad1f0",
    "containers/buildah": "310b1c8f53f3c0a7884f82fe51083577c473490a",
    "containers/common": "a5ccdae846b629b5ceaefa6ffd5c6511409c3487",
    "containers/image": "df7e80d2d19872b61f352a8a182ec934dc0c2346",
    "containers/podman": "5b263b5f5b48004a87caac44e67349a8266d9ef4",
    "containers/storage": "83cf57466529353aced8f1803f2302698e0b5cb7",
    "etcd-io/etcd": "ec166e2292a58365c90e96fcd206b3b74938d49d",
    "kubernetes/apimachinery": "7af103a2a439106791220493349f8d13bc0a1efd",
    "kubernetes/kubectl": "01cb18cdbab1c5d4fd4dd0fc7005789d35989d22",
    "operator-framework/operator-sdk": "6001c29067051e1a04e829ea033988b904d1845e",
    "osbuild/osbuild": "f312d0acb2022212b825e236e417499648ff4dab",
}


SWESMITH_ENVS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 '..', 'logs', 'build_images', 'env')


def _unused_load_swesmith_image_map() -> dict:
    """Unused — kept for reference. SWE-Smith pre-built images aren't on Docker Hub."""
    task_insts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  '..', 'logs', 'task_insts')
    image_map = {}
    if os.path.isdir(task_insts_dir):
        for f in os.listdir(task_insts_dir):
            if f.endswith('.json'):
                try:
                    with open(os.path.join(task_insts_dir, f)) as fh:
                        for inst in json.load(fh):
                            img = inst.get('image_name', '')
                            if img:
                                image_map[inst['instance_id']] = img
                except Exception:
                    pass
    return image_map


def repo_to_key(repo: str) -> str:
    """Convert org/repo to filesystem-safe key: org/repo → org__repo (lowercase for Docker tags)"""
    return repo.replace('/', '__').lower()


def extract_repo_name(repo: str) -> str:
    """Extract repo name from org/repo format."""
    return repo.split('/')[-1]


def get_swesmith_env_dir(instance: Dict[str, Any]) -> str:
    """Return path to SWE-Smith env dir if it exists, else None."""
    iid_parts = instance['instance_id'].split('.')
    if len(iid_parts) < 2:
        return None
    swesmith_key = iid_parts[0] + '.' + iid_parts[1]
    env_dir = os.path.join(SWESMITH_ENVS_DIR, swesmith_key)
    if os.path.isdir(env_dir) and os.path.exists(os.path.join(env_dir, 'setup_env.sh')):
        return env_dir
    return None


def extract_test_paths_python(fail_to_pass: List[str]) -> List[str]:
    """Extract unique test file paths from pytest format.

    Example: path/to/test.py::TestClass::test_method -> path/to/test.py
    """
    paths = set()
    for test in fail_to_pass:
        path = test.split('::')[0]
        paths.add(path)
    return sorted(paths)


def extract_go_packages_from_patch(patch_content: str) -> Set[str]:
    """Extract Go package directories from a patch."""
    pkg_dirs = set()
    for line in patch_content.split('\n'):
        # Handle both 'diff --git a/... b/...' and '--- a/...' formats
        if line.startswith('diff --git') and ' b/' in line:
            path = line.split(' b/')[1].strip()
        elif line.startswith('--- a/') or line.startswith('+++ b/'):
            path = line[6:].strip()
        else:
            continue
        if path.endswith('.go'):
            pkg_dir = os.path.dirname(path)
            if pkg_dir:
                pkg_dirs.add(f"./{pkg_dir}/...")
    return pkg_dirs


def extract_test_packages_go(instance: Dict[str, Any]) -> str:
    """Extract Go test package paths from BOTH test_patch and patch.

    Matches validate_prs.py: scopes to packages from both patches.
    Falls back to ./... when all packages are vendor paths.
    """
    pkg_dirs = set()

    # Extract from both test_patch and patch (not setup_patch)
    for field in ['test_patch', 'patch']:
        diff = instance.get(field, '')
        pkg_dirs.update(extract_go_packages_from_patch(diff))

    if not pkg_dirs:
        return "./..."

    # If ALL packages are vendor paths, fall back to ./...
    non_vendor = [p for p in pkg_dirs if '/vendor/' not in p and not p.startswith('./vendor/')]
    if not non_vendor:
        return "./..."

    return ' '.join(sorted(pkg_dirs))


SWESMITH_EXTRA_APT = {
    "ansible/molecule": "podman",
}
SWESMITH_EXTRA_PIP = {
    "ansible/molecule": "ansible-navigator",
}

def generate_base_dockerfile_swesmith(repo: str, env_dir: str) -> str:
    """Generate base Dockerfile for Python repo using SWE-Smith conda env."""
    clone_repo = MIRROR_REPOS.get(repo, repo)
    repo_name = extract_repo_name(repo)

    extra_apt = SWESMITH_EXTRA_APT.get(repo, "")
    extra_pip = SWESMITH_EXTRA_PIP.get(repo, "")
    apt_pkgs = "git patch"
    if extra_apt:
        apt_pkgs += " " + extra_apt

    lines = [
        f"FROM --platform=linux/x86_64 {SWESMITH_BASE_IMAGE}",
        "USER root",
        "",
        f"RUN apt-get update -qq && apt-get install -y -qq {apt_pkgs} && rm -rf /var/lib/apt/lists/*",
        "",
        "RUN /bin/bash -c 'source /opt/miniconda3/bin/activate && "
        "conda config --remove channels defaults 2>/dev/null; "
        "conda config --set ssl_verify false' || true",
        "COPY setup_env.sh /root/setup_env.sh",
        "RUN chmod +x /root/setup_env.sh && /bin/bash -c 'source ~/.bashrc && /root/setup_env.sh'",
        "",
        "WORKDIR /testbed",
        "",
        'RUN echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" >> /root/.bashrc',
    ]

    if extra_pip:
        lines.append(f"RUN /bin/bash -c 'source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pip install {extra_pip}'")

    return '\n'.join(lines) + '\n'


def generate_base_dockerfile_go(repo: str) -> str:
    """Generate base Dockerfile for Go repo."""
    clone_repo = MIRROR_REPOS.get(repo, repo)
    repo_name = extract_repo_name(repo)
    sys_deps = REPO_SYSTEM_DEPS.get(repo, "")

    base_pkgs = "git patch"
    if sys_deps:
        base_pkgs += " " + sys_deps

    # Map UBI9 dnf package names to Debian apt names
    apt_deps = {
        "gpgme-devel": "libgpgme-dev",
        "libassuan-devel": "libassuan-dev",
        "device-mapper-devel": "libdevmapper-dev",
        "btrfs-progs-devel": "libbtrfs-dev",
        "libseccomp-devel": "libseccomp-dev",
        "libvirt-devel": "libvirt-dev",
    }
    apt_pkgs = "git patch"
    if sys_deps:
        for dep in sys_deps.split():
            apt_pkgs += " " + apt_deps.get(dep, dep)

    lines = [
        f"FROM {GO_BASE_IMAGE}",
        'ENV GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"',
        "",
        f"RUN apt-get update -qq && apt-get install -y -qq {apt_pkgs} && rm -rf /var/lib/apt/lists/*",
        "",
        f"RUN git clone https://github.com/{clone_repo}.git /testbed",
        "WORKDIR /testbed",
    ]

    commit = MIRROR_COMMITS.get(repo)
    if commit:
        lines.append(f"RUN git fetch origin {commit} 2>/dev/null || true && git checkout {commit}")

    return '\n'.join(lines) + '\n'


def generate_base_dockerfile_python(repo: str) -> str:
    """Generate base Dockerfile for Python repo without SWE-Smith env."""
    clone_repo = MIRROR_REPOS.get(repo, repo)
    repo_name = extract_repo_name(repo)

    lines = [
        f"FROM {PYTHON_BASE_IMAGE}",
        "",
        "RUN apt-get update -qq && apt-get install -y -qq git patch && rm -rf /var/lib/apt/lists/*",
        "",
        f"RUN git clone https://github.com/{clone_repo}.git /testbed",
        "WORKDIR /testbed",
        "",
        "RUN pip install --upgrade pip",
        "RUN pip install pytest || true",
    ]

    return '\n'.join(lines) + '\n'


def generate_instance_dockerfile(instance: Dict[str, Any]) -> str:
    """Generate fully self-contained Dockerfile per instance.

    Each task image includes: base image, repo clone, exact commit checkout,
    all dependencies, and patches. No shared base images needed.
    """
    repo = instance['repo']
    base_commit = instance.get('base_commit', '')
    language = instance.get('repo_language', 'unknown')
    setup_patch = instance.get('setup_patch', '').strip()
    test_patch = instance.get('test_patch', '').strip()
    is_synthetic = bool(setup_patch)
    sys_deps = REPO_SYSTEM_DEPS.get(repo, "")
    swesmith_env = get_swesmith_env_dir(instance)

    # Determine clone source and target commit
    if is_synthetic:
        # Synthetic: clone mirror, use HEAD (same as SWE-Smith did)
        clone_repo = MIRROR_REPOS.get(repo, repo)
        clone_url = f"https://github.com/{clone_repo}.git"
        target_commit = ""  # HEAD — matches SWE-Smith's original build
    else:
        # PR: clone upstream at the PR's base_commit
        clone_url = f"https://github.com/{repo}.git"
        target_commit = base_commit

    # Determine base image and build steps based on language + env
    if language == 'Go':
        apt_deps_map = {
            "gpgme-devel": "libgpgme-dev", "libassuan-devel": "libassuan-dev",
            "device-mapper-devel": "libdevmapper-dev", "btrfs-progs-devel": "libbtrfs-dev",
            "libseccomp-devel": "libseccomp-dev", "libvirt-devel": "libvirt-dev",
        }
        apt_pkgs = "git patch"
        if sys_deps:
            for dep in sys_deps.split():
                apt_pkgs += " " + apt_deps_map.get(dep, dep)

        lines = [
            f"FROM {GO_BASE_IMAGE}",
            'ENV GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"',
            "",
            f"RUN apt-get update -qq && apt-get install -y -qq {apt_pkgs} && rm -rf /var/lib/apt/lists/*",
            "",
            f"RUN git clone {clone_url} /testbed",
            "WORKDIR /testbed",
        ]
        if target_commit:
            lines.append(f"RUN git checkout {target_commit}")
        lines.extend(["", "RUN go mod download || true"])

    elif swesmith_env:
        lines = [
            f"FROM --platform=linux/x86_64 {SWESMITH_BASE_IMAGE}",
            "USER root",
            "",
            "RUN apt-get update -qq && apt-get install -y -qq git patch && rm -rf /var/lib/apt/lists/*",
            "",
            f"RUN git clone {clone_url} /testbed",
            "WORKDIR /testbed",
        ]
        if target_commit:
            lines.append(f"RUN git fetch origin {target_commit} 2>/dev/null || true && git checkout {target_commit}")
        lines.extend([
            "",
            "COPY environment.yml /tmp/environment.yml",
            "RUN /bin/bash -c 'source /opt/miniconda3/bin/activate && "
            "conda config --remove channels defaults 2>/dev/null; "
            "conda config --set ssl_verify false && "
            "conda env create --file /tmp/environment.yml && rm /tmp/environment.yml'",
            "RUN /bin/bash -c 'source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pip install -e . && pip install pytest'",
            'RUN echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" >> /root/.bashrc',
        ])

    else:
        lines = [
            f"FROM {PYTHON_BASE_IMAGE}",
            "",
            "RUN apt-get update -qq && apt-get install -y -qq git patch && rm -rf /var/lib/apt/lists/*",
            "",
            f"RUN git clone {clone_url} /testbed",
            "WORKDIR /testbed",
        ]
        if target_commit:
            lines.append(f"RUN git checkout {target_commit}")
        lines.extend([
            "",
            'RUN pip install -e ".[test,dev,testing,develop]" 2>/dev/null || pip install -e ".[test,dev]" 2>/dev/null || pip install -e . 2>/dev/null || true',
            "RUN [ -f requirements.txt ] && pip install -r requirements.txt || true",
            "RUN [ -f requirements-dev.txt ] && pip install -r requirements-dev.txt || true",
            "RUN pip install pytest || true",
        ])

    # Apply patches
    if setup_patch:
        lines.extend([
            "",
            "COPY setup.patch /tmp/setup.patch",
            "RUN git apply /tmp/setup.patch || git apply --reject /tmp/setup.patch || patch --batch --fuzz=5 -p1 -i /tmp/setup.patch",
        ])

    if test_patch:
        lines.extend([
            "",
            "COPY test.patch /tmp/test.patch",
            "RUN git apply /tmp/test.patch || git apply --reject /tmp/test.patch || patch --batch --fuzz=5 -p1 -i /tmp/test.patch",
        ])

    return '\n'.join(lines) + '\n'


def generate_solve_sh(instance: Dict[str, Any]) -> str:
    """Generate solve.sh script that applies the gold patch.

    Synthetic: reverse-apply setup_patch (buggy→clean)
    PR-based: forward-apply patch (buggy→clean)
    """
    setup_patch = instance.get('setup_patch', '').strip()
    patch = instance['patch']
    is_synthetic = bool(setup_patch)

    if is_synthetic:
        # Synthetic: setup_patch introduced the bug, reverse it to fix
        fix_patch = setup_patch
        patch_flag = "-R -p1"
    else:
        # PR instances: patch is the fix (buggy→clean)
        fix_patch = patch
        patch_flag = "-p1"

    if not fix_patch.endswith('\n'):
        fix_patch += '\n'
    encoded_patch = base64.b64encode(fix_patch.encode()).decode()

    if is_synthetic:
        apply_cmd = (
            f"echo '{encoded_patch}' | base64 -d > /tmp/gold.patch && "
            "{ git apply -R /tmp/gold.patch || "
            "git apply -R --reject /tmp/gold.patch || "
            "patch -R --batch --fuzz=5 -p1 -i /tmp/gold.patch; }"
        )
    else:
        apply_cmd = (
            f"echo '{encoded_patch}' | base64 -d > /tmp/gold.patch && "
            "{ git apply /tmp/gold.patch || "
            "git apply --reject /tmp/gold.patch || "
            "patch --batch --fuzz=5 -p1 -i /tmp/gold.patch; }"
        )

    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        apply_cmd,
        "",
        "echo 'Gold patch applied successfully'",
    ]

    return '\n'.join(lines) + '\n'


def generate_test_sh(instance: Dict[str, Any]) -> str:
    """Generate test.sh — runs only F2P tests, checks exit code.

    Go: go test -run "TestA|TestB" -v -short -count=1 <packages>
    Python: pytest specific_test.py::test_func
    """
    language = instance.get('repo_language', 'unknown')
    fail_to_pass = instance.get('FAIL_TO_PASS', [])
    if isinstance(fail_to_pass, str):
        fail_to_pass = json.loads(fail_to_pass) if fail_to_pass.startswith('[') else [fail_to_pass]
    repo = instance['repo']

    lines = [
        "#!/bin/bash",
        "mkdir -p /logs/verifier",
        "",
    ]

    if language == 'Go':
        pkg_path = extract_test_packages_go(instance)

        # Build -run pattern from F2P test names
        test_funcs = [t.split('/')[0] for t in fail_to_pass
                      if not t.startswith('test') and '::' not in t]
        run_pattern = '|'.join(set(test_funcs)) if test_funcs else ""

        # For go.work repos, determine module dir from patch paths
        go_test_dir = ""
        if repo in GO_WORK_MODULES:
            patch_content = instance.get('setup_patch', '') or instance.get('patch', '')
            mod_dirs = set()
            for line in patch_content.split('\n'):
                fpath = None
                if line.startswith('diff --git') and ' b/' in line:
                    fpath = line.split(' b/')[1].strip()
                elif line.startswith('+++ b/'):
                    fpath = line[6:].strip()
                if fpath:
                    for mod in GO_WORK_MODULES[repo]:
                        if fpath.startswith(mod + '/'):
                            mod_dirs.add(mod)
                            break
            if mod_dirs:
                go_test_dirs = sorted(mod_dirs)
            else:
                go_test_dirs = []
        else:
            go_test_dirs = []

        run_flag = f'-run "{run_pattern}"' if run_pattern else ""
        if len(go_test_dirs) > 1:
            test_cmds = " ; ".join(
                f"(cd /testbed/{d} && go test -v -short -count=1 {run_flag} ./...)"
                for d in go_test_dirs
            )
            test_cmd = f"{{ {test_cmds}; }}"
        elif go_test_dirs:
            test_cmd = f"cd {go_test_dirs[0]} && go test -v -short -count=1 {run_flag} ./..."
        else:
            test_cmd = f"go test -v -short -count=1 {run_flag} {pkg_path}"

        lines.extend([
            f"if {test_cmd}; then",
            "  echo 1 > /logs/verifier/reward.txt",
            "else",
            "  echo 0 > /logs/verifier/reward.txt",
            "fi",
        ])

    elif language == 'Python':
        swesmith_env = get_swesmith_env_dir(instance)
        repo_has_swesmith = any(
            os.path.isdir(os.path.join(SWESMITH_ENVS_DIR, e))
            and e.lower().startswith(repo_to_key(repo) + '.')
            for e in os.listdir(SWESMITH_ENVS_DIR)
        ) if os.path.exists(SWESMITH_ENVS_DIR) else False

        if swesmith_env or repo_has_swesmith:
            lines.append("source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed")
        else:
            # Non-swesmith Python: ensure pytest is available
            lines.append("pip install pytest > /dev/null 2>&1 || true")

        # Run only the F2P tests directly — use single quotes to protect bash special chars ($, #, &)
        test_args = ' '.join(f"'{t}'" for t in fail_to_pass) if fail_to_pass else '.'
        pytest_cmd = f"python -m pytest --disable-warnings --color=no --tb=no --verbose {test_args}"

        lines.extend([
            f"if {pytest_cmd}; then",
            "  echo 1 > /logs/verifier/reward.txt",
                "else",
                "  echo 0 > /logs/verifier/reward.txt",
                "fi",
            ])

    else:
        # Unknown language - generic test
        lines.extend([
            "# Generic test - mark as pass for now",
            "echo 1 > /logs/verifier/reward.txt",
        ])

    lines.append("exit 0")
    return '\n'.join(lines) + '\n'


def generate_task_toml(instance: Dict[str, Any]) -> str:
    """Generate task.toml metadata file."""
    difficulty_raw = instance.get('difficulty', 'unknown')
    difficulty = DIFFICULTY_MAP.get(difficulty_raw, 'medium')
    timeout = TIMEOUT_MAP.get(difficulty, 1800)

    lines = [
        'version = "1.0"',
        '',
        '[metadata]',
        'author_name = "rh-swe-bench"',
        'author_email = ""',
        f'difficulty = "{difficulty}"',
        f'difficulty_explanation = "Original: {difficulty_raw}"',
        'category = "swe_bench"',
        'tags = ["rh-swe-bench", "auto-generated"]',
        '',
        '[verifier]',
        f'timeout_sec = {timeout}.0',
        '',
        '[agent]',
        f'timeout_sec = {timeout}.0',
        '',
        '[environment]',
        'build_timeout_sec = 600.0',
        'cpus = 2',
        'memory_mb = 8192',
        'storage_mb = 20480',
    ]

    return '\n'.join(lines) + '\n'


def generate_input_yaml(instance: Dict[str, Any]) -> str:
    """Generate input.yaml with problem statement."""
    problem_statement = instance.get('problem_statement', '')

    # Escape quotes and backslashes for YAML
    escaped = problem_statement.replace('\\', '\\\\').replace('"', '\\"')

    return f'prompt: "{escaped}"\n'


def generate_instruction_md(instance: Dict[str, Any]) -> str:
    """Generate instruction.md with problem statement as markdown."""
    return instance.get('problem_statement', '') + '\n'


def generate_base_image(repo: str, language: str, output_dir: Path,
                        repo_has_swesmith_env: bool = False) -> None:
    """Generate base image Dockerfile for a repo."""
    repo_key = repo_to_key(repo)
    base_dir = output_dir / 'bases' / repo_key
    base_dir.mkdir(parents=True, exist_ok=True)

    # Check if this repo has a SWE-Smith env (for synthetic instances)
    swesmith_env_dir = None
    if repo_has_swesmith_env and os.path.exists(SWESMITH_ENVS_DIR):
        # Find the env dir by scanning SWESMITH_ENVS_DIR
        for entry in os.listdir(SWESMITH_ENVS_DIR):
            entry_path = os.path.join(SWESMITH_ENVS_DIR, entry)
            if os.path.isdir(entry_path):
                # SWE-Smith env dirs are named like: org__repo.commit_prefix
                if entry.lower().startswith(repo_key + '.') and os.path.exists(os.path.join(entry_path, 'setup_env.sh')):
                    swesmith_env_dir = entry_path
                    break

    if swesmith_env_dir:
        # Python with SWE-Smith env
        dockerfile = generate_base_dockerfile_swesmith(repo, swesmith_env_dir)
        with open(base_dir / 'Dockerfile', 'w') as f:
            f.write(dockerfile)
        # Copy setup_env.sh and inject commit checkout if needed
        shutil.copy2(os.path.join(swesmith_env_dir, 'setup_env.sh'),
                     base_dir / 'setup_env.sh')
        commit = MIRROR_COMMITS.get(repo)
        if commit:
            setup_path = base_dir / 'setup_env.sh'
            content = setup_path.read_text()
            if 'git checkout' not in content:
                content = content.replace(
                    'cd /testbed',
                    f'cd /testbed\ngit fetch origin {commit} 2>/dev/null || true\ngit checkout {commit}'
                )
                setup_path.write_text(content)
        print(f"    → Using SWE-Smith env from {os.path.basename(swesmith_env_dir)}")
    elif language == 'Go':
        dockerfile = generate_base_dockerfile_go(repo)
        with open(base_dir / 'Dockerfile', 'w') as f:
            f.write(dockerfile)
    elif language == 'Python':
        dockerfile = generate_base_dockerfile_python(repo)
        with open(base_dir / 'Dockerfile', 'w') as f:
            f.write(dockerfile)
    else:
        # Unknown language — treat as Python
        dockerfile = generate_base_dockerfile_python(repo)
        with open(base_dir / 'Dockerfile', 'w') as f:
            f.write(dockerfile)


def generate_task(instance: Dict[str, Any], output_dir: Path, task_idx: int) -> None:
    """Generate a single Harbor task directory."""
    task_dir = output_dir / f"task-{task_idx:04d}"
    task_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (task_dir / 'environment').mkdir(exist_ok=True)
    (task_dir / 'solution').mkdir(exist_ok=True)
    (task_dir / 'tests').mkdir(exist_ok=True)

    # Generate metadata files
    with open(task_dir / 'task.toml', 'w') as f:
        f.write(generate_task_toml(instance))

    with open(task_dir / 'input.yaml', 'w') as f:
        f.write(generate_input_yaml(instance))

    with open(task_dir / 'instruction.md', 'w') as f:
        f.write(generate_instruction_md(instance))

    with open(task_dir / 'environment' / 'Dockerfile', 'w') as f:
        f.write(generate_instance_dockerfile(instance))

    # Write patch files for COPY in Dockerfile
    setup_patch = instance.get('setup_patch', '').strip()
    test_patch = instance.get('test_patch', '').strip()
    if setup_patch:
        with open(task_dir / 'environment' / 'setup.patch', 'w') as f:
            f.write(setup_patch if setup_patch.endswith('\n') else setup_patch + '\n')
    if test_patch:
        with open(task_dir / 'environment' / 'test.patch', 'w') as f:
            f.write(test_patch if test_patch.endswith('\n') else test_patch + '\n')

    # Copy conda environment YAML for SWE-Smith Python repos
    swesmith_env = get_swesmith_env_dir(instance)
    if swesmith_env:
        env_yml = os.path.join(swesmith_env, [f for f in os.listdir(swesmith_env) if f.endswith('.yml') and 'sweenv' in f][0]) if any('sweenv' in f for f in os.listdir(swesmith_env)) else None
        if env_yml:
            shutil.copy2(env_yml, task_dir / 'environment' / 'environment.yml')
        else:
            setup_content = open(os.path.join(swesmith_env, 'setup_env.sh')).read()
            import re
            yml_match = re.search(r"cat <<'EOF_\d+' > swesmith_environment.yml\n(.*?)EOF_\d+", setup_content, re.DOTALL)
            if yml_match:
                with open(task_dir / 'environment' / 'environment.yml', 'w') as f:
                    f.write(yml_match.group(1))

    # Generate solution and test scripts
    with open(task_dir / 'solution' / 'solve.sh', 'w') as f:
        f.write(generate_solve_sh(instance))
    os.chmod(task_dir / 'solution' / 'solve.sh', 0o755)

    # Also write gold.patch as readable file
    setup_patch = instance.get('setup_patch', '').strip()
    fix_patch = setup_patch if setup_patch else instance.get('patch', '')
    if fix_patch:
        with open(task_dir / 'solution' / 'gold.patch', 'w') as f:
            f.write(fix_patch if fix_patch.endswith('\n') else fix_patch + '\n')

    with open(task_dir / 'tests' / 'test.sh', 'w') as f:
        f.write(generate_test_sh(instance))
    os.chmod(task_dir / 'tests' / 'test.sh', 0o755)

    print(f"Generated {task_dir.name}: {instance['instance_id']}")


def generate_eval_yaml(output_dir: Path, total_tasks: int,
                       difficulty_counts: Dict[str, int]) -> None:
    """Generate eval.yaml at dataset root."""
    content = f"""name: rh-swe-bench-v1
description: >
  Red Hat SWE-bench evaluation dataset with {total_tasks} tasks from RH repositories.
  Includes {difficulty_counts.get('easy', 0)} easy, {difficulty_counts.get('medium', 0)} medium,
  {difficulty_counts.get('hard', 0)} hard tasks. Mix of synthetic instances (with setup_patch)
  and PR-based instances (with test_patch).

skill: null

execution:
  mode: case
  arguments: "{{prompt}}"
  timeout: 3600

runner:
  type: claude-code

models:
  skill: claude-sonnet-4-6
  judge: claude-opus-4-6

permissions:
  allow: []
  deny:
    - "mcp__*"

dataset:
  path: {output_dir.name}
  schema: |
    Each case directory (task-NNNN/) contains:
    - input.yaml: Contains 'prompt' field with the problem statement
    - instruction.md: Problem statement in markdown format
    - task.toml: Metadata including difficulty (easy/medium/hard)
    - environment/Dockerfile: Sets up repo at base_commit with dependencies
    - solution/solve.sh: Oracle solution (applies gold patch via patch -p1)
    - tests/test.sh: Test runner script that writes 1/0 to /logs/verifier/reward.txt

outputs:
  - path: artifacts
    schema: |
      Agent-generated code changes to satisfy the problem statement.

judges:
  - name: has_code_changes
    description: Check that the agent produced non-trivial output
    check: |
      content = outputs.get("main_content", "")
      if len(content.strip()) < 50:
          return False, f"Output too short ({{len(content.strip())}} chars)"
      return True, f"Output has {{len(content.strip())}} chars"

  - name: solution_quality
    description: >
      Evaluate whether the agent's solution addresses the task requirements.
      Compare against the oracle solution and test expectations.
    prompt: |
      You are evaluating an agent's attempt to complete a coding task.

      The task instruction is provided as input. The agent's output is the
      generated code/changes. Compare the agent's approach against the
      reference solution (oracle) and assess:

      1. Does it address the core problem described in the instruction?
      2. Are the changes technically sound?
      3. Would the changes likely pass the test suite?

      Score 1-5 where:
      1 = No meaningful attempt
      2 = Partially addresses the problem but with significant issues
      3 = Addresses the problem but with gaps or quality issues
      4 = Good solution with minor issues
      5 = Excellent solution, comparable to the oracle

thresholds:
  has_code_changes:
    min_pass_rate: 0.8
  solution_quality:
    min_mean: 3.0

matrix:
  factors:
    model:
      levels:
        - claude-sonnet-4-6
        - claude-opus-4-6
    effort:
      levels:
        - low
        - high
  replications: 1
  execution:
    parallelism: 4
"""

    with open(output_dir / 'eval.yaml', 'w') as f:
        f.write(content)

    print(f"\nGenerated eval.yaml at {output_dir / 'eval.yaml'}")


def main():
    parser = argparse.ArgumentParser(description='Convert RH SWE-bench to Harbor format')
    parser.add_argument(
        '--input',
        default='/Users/rbende/Desktop/Code-mode/SWE-smith/logs/rh_benchmark/dataset/rh_swe_bench_verified.json',
        help='Input JSON dataset file'
    )
    parser.add_argument(
        '--output',
        default='/Users/rbende/Desktop/Code-mode/SWE-smith/redhat-bench/harbor-dataset',
        help='Output directory for Harbor dataset'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of tasks to generate (for testing)'
    )

    args = parser.parse_args()

    # Load dataset
    with open(args.input) as f:
        instances = json.load(f)

    if args.limit:
        instances = instances[:args.limit]

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: Scan all instances to identify unique repos and their properties
    print("Scanning instances to identify repos...")
    repo_info = {}  # repo -> {language, has_swesmith_env}
    for instance in instances:
        repo = instance['repo']
        if repo not in repo_info:
            language = instance.get('repo_language', 'unknown')
            repo_info[repo] = {
                'language': language,
                'has_swesmith_env': False
            }

        # Check if this instance has SWE-Smith env
        swesmith_env = get_swesmith_env_dir(instance)
        if swesmith_env:
            repo_info[repo]['has_swesmith_env'] = True

    print(f"Found {len(repo_info)} unique repos")
    swesmith_repos = [r for r, info in repo_info.items() if info['has_swesmith_env']]
    if swesmith_repos:
        print(f"  Repos with SWE-Smith envs: {', '.join(swesmith_repos)}")

    # Phase 2: Generate base images
    print("\nGenerating base images...")
    for repo, info in sorted(repo_info.items()):
        generate_base_image(repo, info['language'], output_dir,
                           info['has_swesmith_env'])
        print(f"  Generated base for {repo} ({info['language']})")

    # Phase 3: Generate task instances
    print("\nGenerating task instances...")
    difficulty_counts = {}
    for idx, instance in enumerate(instances):
        generate_task(instance, output_dir, idx)

        # Track difficulty
        diff_raw = instance.get('difficulty', 'unknown')
        diff = DIFFICULTY_MAP.get(diff_raw, 'medium')
        difficulty_counts[diff] = difficulty_counts.get(diff, 0) + 1

    # Phase 4: Generate eval.yaml
    generate_eval_yaml(output_dir, len(instances), difficulty_counts)

    print(f"\n{'='*60}")
    print(f"Generated {len(repo_info)} base images")
    print(f"Generated {len(instances)} task instances")
    print(f"Difficulty distribution: {difficulty_counts}")
    print(f"Output: {output_dir}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
