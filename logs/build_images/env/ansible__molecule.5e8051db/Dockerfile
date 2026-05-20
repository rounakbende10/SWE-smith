# syntax=docker/dockerfile:1
FROM --platform=linux/x86_64 swebench/swesmith.x86_64
ENV GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"

COPY ./setup_env.sh /root/
RUN --mount=type=ssh,required=false sed -i -e 's/\r$//' /root/setup_env.sh
RUN --mount=type=ssh,required=false chmod +x /root/setup_env.sh
RUN --mount=type=ssh,required=false /bin/bash -c "source ~/.bashrc && /root/setup_env.sh"

WORKDIR /testbed/

# Automatically activate the testbed environment
RUN --mount=type=ssh,required=false echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" > /root/.bashrc
