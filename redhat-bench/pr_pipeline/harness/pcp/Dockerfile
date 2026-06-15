FROM fedora:39

# PCP build + test dependencies (no Qt GUI - not needed for qa tests)
RUN dnf install -y \
    git gcc gcc-c++ make autoconf automake libtool \
    python3-devel python3-pip \
    avahi-devel systemd-devel \
    librados-devel librbd-devel \
    cyrus-sasl-devel openssl-devel \
    perl-ExtUtils-MakeMaker perl-JSON perl-YAML perl-Time-HiRes \
    flex bison \
    rsync patch diffutils \
    bc time hostname net-tools procps-ng \
    nmap-ncat socat \
    pcp pcp-testsuite python3-pcp \
    && dnf clean all

# Clone full PCP repo (need full history for per-commit checkouts)
RUN git clone https://github.com/performancecopilot/pcp /testbed
WORKDIR /testbed

# Harness scripts
COPY setup_pcp.sh /harness/setup_pcp.sh
COPY eval_pcp.sh /harness/eval_pcp.sh
RUN chmod +x /harness/*.sh

ENV PCPQA_SYSTEMD=yes
ENV PCP_SYSTEMDUNIT_DIR=/usr/lib/systemd/system
