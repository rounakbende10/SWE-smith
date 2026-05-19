import re

from dataclasses import dataclass, field
from swebench.harness.constants import TestStatus
from swesmith.constants import ENV_NAME
from swesmith.profiles.base import RepoProfile, registry


@dataclass
class CProfile(RepoProfile):
    """
    Profile for C repositories.
    """

    exts: list[str] = field(default_factory=lambda: [".c"])


@dataclass
class Jqb9e19de76(CProfile):
    owner: str = "jqlang"
    repo: str = "jq"
    commit: str = "b9e19de76e6e19d044007ead65d164710dc98877"
    test_cmd: str = "make check"
    eval_sets: set[str] = field(
        default_factory=lambda: {"SWE-bench/SWE-bench_Multilingual"}
    )

    @property
    def dockerfile(self):
        return f"""FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive \
    DEBCONF_NONINTERACTIVE_SEEN=true \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8
ENV TZ=Etc/UTC
RUN apt-get update \
    && apt-get install -y build-essential autoconf libtool git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/{self.mirror_name} /{ENV_NAME}
WORKDIR /{ENV_NAME}
RUN git submodule update --init --recursive
RUN autoreconf -i \
    && ./configure \
    --disable-docs \
    --with-oniguruma=builtin \
    --enable-static \
    --enable-all-static \
    --prefix=/usr/local
RUN make clean
RUN touch src/parser.y src/lexer.l
RUN make -j$(nproc)
"""

    def log_parser(self, log: str) -> dict[str, str]:
        test_status_map = {}
        pattern = r"^\s*(PASS|FAIL):\s(.+)$"
        for line in log.split("\n"):
            match = re.match(pattern, line.strip())
            if match:
                status, test_name = match.groups()
                if status == "PASS":
                    test_status_map[test_name] = TestStatus.PASSED.value
                elif status == "FAIL":
                    test_status_map[test_name] = TestStatus.FAILED.value
        return test_status_map


@dataclass
class Valkeyfc7c04e4(CProfile):
    owner: str = "valkey-io"
    repo: str = "valkey"
    commit: str = "fc7c04e4f8ba86dfbac1ec059db457fb44ed0a2d"
    test_cmd: str = "TERM=dumb ./runtest --durable"
    eval_sets: set[str] = field(
        default_factory=lambda: {"SWE-bench/SWE-bench_Multilingual"}
    )

    @property
    def dockerfile(self):
        return f"""FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN sed -i 's/^# deb-src/deb-src/' /etc/apt/sources.list
RUN apt update && \
    apt install -y pkg-config wget git build-essential libtool automake autoconf tcl bison flex cmake python3 python3-pip python3-venv python-is-python3 && \
    rm -rf /var/lib/apt/lists/*
RUN adduser --disabled-password --gecos 'dog' nonroot
RUN git clone https://github.com/{self.mirror_name} /{ENV_NAME}
WORKDIR /{ENV_NAME}
RUN cd deps/jemalloc && ./autogen.sh
RUN make distclean
RUN make
"""

    def log_parser(self, log: str) -> dict[str, str]:
        test_status_map = {}
        pattern = r"^\[(ok|err|skip|ignore)\]:\s(.+?)(?:\s\((\d+\s*m?s)\))?$"
        for line in log.split("\n"):
            match = re.match(pattern, line.strip())
            if match:
                status, test_name, _duration = match.groups()
                if status == "ok":
                    test_status_map[test_name] = TestStatus.PASSED.value
                elif status == "err":
                    # Strip out file path information from failed test names
                    test_name = re.sub(r"\s+in\s+\S+$", "", test_name)
                    test_status_map[test_name] = TestStatus.FAILED.value
                elif status == "skip" or status == "ignore":
                    test_status_map[test_name] = TestStatus.SKIPPED.value
        return test_status_map


import os
import subprocess
import shutil

RH_GH_ORG_C = "rounakbende10"


@dataclass
class Systemd1006535b(CProfile):
    """systemd/systemd — RHEL init system and service manager.

    Core RHEL component. C codebase with meson build system.
    Tests via meson test. Some tests are self-contained unit tests.
    """

    owner: str = "systemd"
    repo: str = "systemd"
    commit: str = "1006535b"
    org_gh: str = RH_GH_ORG_C
    test_cmd: str = "meson test -C build --no-rebuild --print-errorlogs --timeout-multiplier=3"
    timeout: int = 600
    eval_sets: set[str] = field(
        default_factory=lambda: {"SWE-bench/SWE-bench_RedHat"}
    )

    @property
    def dockerfile(self):
        return f"""FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git meson ninja-build gcc pkg-config \
    libcap-dev libmount-dev libseccomp-dev libblkid-dev libkmod-dev \
    gperf python3-jinja2 libglib2.0-dev liblz4-dev libzstd-dev \
    libfdisk-dev libp11-kit-dev libssl-dev libgcrypt20-dev && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/{self.mirror_name} /{ENV_NAME}
WORKDIR /{ENV_NAME}
RUN meson setup build -Dtests=true || true
"""

    def log_parser(self, log: str) -> dict[str, str]:
        test_status_map = {}
        for line in log.split("\n"):
            if "OK" in line and "::" in line:
                test_name = line.split("::")[0].strip().split()[-1]
                test_status_map[test_name] = "PASSED"
            elif "FAIL" in line and "::" in line:
                test_name = line.split("::")[0].strip().split()[-1]
                test_status_map[test_name] = "FAILED"
        return test_status_map

    def create_mirror(self):
        if self._mirror_exists():
            return
        if self.repo_name in os.listdir():
            shutil.rmtree(self.repo_name)
        source_repo = self.api.repos.get(self.owner, self.repo)
        self.api.repos.create_for_authenticated_user(
            name=self.repo_name, private=source_repo.private)
        self._configure_ssh_env()
        subprocess.run(f"git clone {self._source_read_url} {self.repo_name}",
            shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git_cmds = [f"cd {self.repo_name}", f"git checkout {self.commit}",
            "rm -rf .git", "git init", 'git config user.name "swesmith"',
            'git config user.email "swesmith@anon.com"', "rm -rf .github/workflows",
            "mv .gitignore .gitignore.bak 2>/dev/null; true", "git add .",
            "mv .gitignore.bak .gitignore 2>/dev/null; true",
            "git add -f .gitignore 2>/dev/null; true",
            "git commit --no-gpg-sign -m 'Initial commit'", "git branch -M main",
            f"git remote add origin git@github.com:{self.mirror_name}.git",
            "git push -u origin main"]
        subprocess.run("; ".join(git_cmds), shell=True, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(f"rm -rf {self.repo_name}", shell=True, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Register all C profiles with the global registry
for name, obj in list(globals().items()):
    if (
        isinstance(obj, type)
        and issubclass(obj, CProfile)
        and obj.__name__ != "CProfile"
    ):
        registry.register_profile(obj)
