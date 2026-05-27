"""
Auto-detect and build reproducible test environments for Python repos.

Resolves dependencies on the runner (which has internet), then builds an offline
Docker image with everything pre-installed.

Usage:
    from auto_env import build_repo_image
    tag = build_repo_image("RedHatInsights", "insights-core", docker_host="tcp://localhost:2375")
"""
import json
import os
import re
import subprocess
import shutil

# Common system packages needed by popular Python C extensions
SYSTEM_DEPS = [
    "build-essential", "libffi-dev", "libssl-dev", "libxml2-dev", "libxslt-dev",
    "libyaml-dev", "libpq-dev", "libjpeg-dev", "zlib1g-dev", "libcurl4-openssl-dev",
    "pkg-config", "cmake",
]

# Target platform for pip download (must match Docker base image)
PIP_PLATFORM_FLAGS = (
    "--platform manylinux2014_x86_64 "
    "--platform manylinux_2_17_x86_64 "
    "--platform linux_x86_64 "
    "--python-version {pyver} "
    "--implementation cp "
    "--abi cp{pyver} "
    "--only-binary=:all:"
)

_failed_repos = set()


def _detect_extras(clone_dir):
    """Read setup.cfg/pyproject.toml/setup.py to find available extras."""
    extras = []

    # Check pyproject.toml
    toml_path = os.path.join(clone_dir, "pyproject.toml")
    if os.path.exists(toml_path):
        content = open(toml_path).read()
        for m in re.finditer(r'\[project\.optional-dependencies\]\s*\n(.*?)(?:\n\[|\Z)', content, re.DOTALL):
            for line in m.group(1).split("\n"):
                key = line.split("=")[0].strip()
                if key and not key.startswith("#"):
                    extras.append(key)

    # Check setup.cfg
    cfg_path = os.path.join(clone_dir, "setup.cfg")
    if os.path.exists(cfg_path):
        content = open(cfg_path).read()
        in_extras = False
        for line in content.split("\n"):
            if line.strip() == "[options.extras_require]":
                in_extras = True
                continue
            if in_extras:
                if line.startswith("["):
                    break
                key = line.split("=")[0].strip()
                if key and not key.startswith("#"):
                    extras.append(key)

    # Check setup.py for extras_require
    setup_path = os.path.join(clone_dir, "setup.py")
    if os.path.exists(setup_path):
        content = open(setup_path).read()
        for m in re.finditer(r"['\"](\w+)['\"]:\s*(?:list\(|set\(|\[)", content):
            extras.append(m.group(1))

    # Filter to valid extras names only (alphanumeric, hyphens, underscores)
    clean = []
    for e in set(extras):
        e = e.strip().strip("'\"`,[]")
        if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', e):
            clean.append(e)
    return clean


def _detect_test_runner(clone_dir):
    """Detect which test runner the repo uses."""
    files = os.listdir(clone_dir)

    if "tox.ini" in files:
        tox_content = open(os.path.join(clone_dir, "tox.ini")).read()
        if "pytest" in tox_content:
            return "python -m pytest --disable-warnings --color=no --tb=no --verbose"

    if "pytest.ini" in files or "pyproject.toml" in files:
        pyproject = os.path.join(clone_dir, "pyproject.toml")
        if os.path.exists(pyproject) and "pytest" in open(pyproject).read():
            return "python -m pytest --disable-warnings --color=no --tb=no --verbose"

    if "setup.cfg" in files:
        cfg = open(os.path.join(clone_dir, "setup.cfg")).read()
        if "[tool:pytest]" in cfg or "pytest" in cfg:
            return "python -m pytest --disable-warnings --color=no --tb=no --verbose"

    # Default to pytest
    return "python -m pytest --disable-warnings --color=no --tb=no --verbose"


def _find_test_extras(extras):
    """Pick the best extras for installing test dependencies."""
    # Priority order
    for pattern in ["test", "testing", "dev", "develop", "all"]:
        matches = [e for e in extras if pattern in e.lower()]
        if matches:
            return matches

    return []


def _download_wheels(clone_dir, pip_dir, extras, pyver="312"):
    """Download platform-specific wheels for all dependencies."""
    os.makedirs(pip_dir, exist_ok=True)
    flags = PIP_PLATFORM_FLAGS.format(pyver=pyver)

    # Try with test extras first
    test_extras = _find_test_extras(extras)
    if test_extras:
        extras_str = ",".join(test_extras)
        subprocess.run(
            f'pip download -d {pip_dir} {flags} "{clone_dir}/.[{extras_str}]"',
            shell=True, capture_output=True, timeout=120
        )

    # Always download base deps
    subprocess.run(
        f'pip download -d {pip_dir} {flags} "{clone_dir}/."',
        shell=True, capture_output=True, timeout=120
    )

    # Always include pytest, build tools, and common transitive deps
    subprocess.run(
        f"pip download -d {pip_dir} {flags} "
        f"pytest setuptools wheel setuptools-scm typing-extensions "
        f"tomli exceptiongroup coverage pytest-cov",
        shell=True, capture_output=True, timeout=60
    )

    return len([f for f in os.listdir(pip_dir) if f.endswith(('.whl', '.tar.gz'))])


def build_repo_image(owner, repo, pyver="312", py_image="python:3.12-bookworm", docker_host=None):
    """
    Build a Docker image for a Python repo with all dependencies pre-installed.
    Returns the image tag, or None if it fails.
    """
    repo_key = f"{owner}/{repo}"
    if repo_key in _failed_repos:
        return None

    tag = f"base-{owner}-{repo}".lower()

    # Check if already built
    env = os.environ.copy()
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    r = subprocess.run(f"docker inspect {tag}", shell=True, capture_output=True, env=env)
    if r.returncode == 0:
        return tag

    print(f"    [auto_env] Setting up {owner}/{repo}...", flush=True)

    clone_dir = f"/tmp/auto_clone_{owner}_{repo}"
    pip_dir = f"/tmp/auto_pkgs_{owner}_{repo}"
    build_dir = f"/tmp/auto_build_{owner}_{repo}"

    try:
        # Clone
        shutil.rmtree(clone_dir, ignore_errors=True)
        r = subprocess.run(
            f"git clone --depth 1 https://github.com/{owner}/{repo} {clone_dir}",
            shell=True, capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            print(f"    [auto_env] Clone failed: {r.stderr[-200:]}", flush=True)
            _failed_repos.add(repo_key)
            return None

        # Detect extras and test runner
        extras = _detect_extras(clone_dir)
        test_runner = _detect_test_runner(clone_dir)
        test_extras = _find_test_extras(extras)
        print(f"    [auto_env] Extras: {extras}", flush=True)
        print(f"    [auto_env] Test extras: {test_extras}", flush=True)
        print(f"    [auto_env] Test runner: {test_runner}", flush=True)

        # Download wheels
        shutil.rmtree(pip_dir, ignore_errors=True)
        pkg_count = _download_wheels(clone_dir, pip_dir, extras, pyver)
        print(f"    [auto_env] Downloaded {pkg_count} packages", flush=True)

        if pkg_count == 0:
            print(f"    [auto_env] No packages downloaded — skipping", flush=True)
            _failed_repos.add(repo_key)
            return None

        # Build Docker image
        shutil.rmtree(build_dir, ignore_errors=True)
        os.makedirs(build_dir)
        shutil.copytree(pip_dir, os.path.join(build_dir, "pkgs"))

        # Construct pip install command with detected extras
        if test_extras:
            extras_str = ",".join(test_extras)
            install_cmd = (
                f'pip install --no-index --find-links=/pkgs /pkgs/*.whl && '
                f'pip install --no-index --find-links=/pkgs -e ".[{extras_str}]"'
            )
        else:
            install_cmd = (
                f'pip install --no-index --find-links=/pkgs /pkgs/*.whl && '
                f'pip install --no-index --find-links=/pkgs -e .'
            )

        sys_deps_str = " ".join(SYSTEM_DEPS)
        dockerfile = f"""FROM {py_image}
RUN apt-get update -qq && apt-get install -y -qq git {sys_deps_str} > /dev/null 2>&1 || true
COPY pkgs /pkgs
RUN git clone https://github.com/{owner}/{repo} /testbed
WORKDIR /testbed
RUN {install_cmd}
"""
        with open(os.path.join(build_dir, "Dockerfile"), "w") as f:
            f.write(dockerfile)

        print(f"    [auto_env] Building image...", flush=True)
        r = subprocess.run(
            f"docker build --platform linux/x86_64 -t {tag} {build_dir}",
            shell=True, capture_output=True, text=True, timeout=900, env=env
        )

        if r.returncode != 0:
            print(f"    [auto_env] Build failed: {r.stderr[-300:]}", flush=True)
            _failed_repos.add(repo_key)
            return None

        # Verify pytest works
        verify = subprocess.run(
            f"docker run --rm {tag} python -m pytest --version",
            shell=True, capture_output=True, text=True, timeout=30, env=env
        )
        if verify.returncode != 0:
            print(f"    [auto_env] WARNING: pytest not available in image", flush=True)

        print(f"    [auto_env] Image ready: {tag}", flush=True)
        return tag

    except subprocess.TimeoutExpired:
        print(f"    [auto_env] Timeout building {owner}/{repo}", flush=True)
        _failed_repos.add(repo_key)
        return None
    except Exception as e:
        print(f"    [auto_env] Error: {e}", flush=True)
        _failed_repos.add(repo_key)
        return None
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
        shutil.rmtree(pip_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)


def detect_test_command(clone_dir):
    """Public API: detect the test command for a repo."""
    return _detect_test_runner(clone_dir)
