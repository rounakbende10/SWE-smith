#!/bin/bash
set -euxo pipefail
git clone -o origin https://github.com/rounakbende10/osbuild__osbuild.f312d0ac /testbed
cd /testbed
source /opt/miniconda3/bin/activate
cat <<'EOF_59812759871' > swesmith_environment.yml

EOF_59812759871
conda env create --file swesmith_environment.yml
conda activate testbed && conda install python=3.12 -y
rm swesmith_environment.yml
conda activate testbed
echo "Current environment: $CONDA_DEFAULT_ENV"
pip install -e .
pip install pytest
