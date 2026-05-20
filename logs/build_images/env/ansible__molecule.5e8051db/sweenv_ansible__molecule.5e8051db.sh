#!/bin/bash

git clone https://github.com/ansible/molecule.git
git checkout 5e8051db
#!/bin/bash
set -e
conda create -n testbed python=3.12 -y
conda activate testbed || source activate testbed
pip install -e .
pip install pytest pytest-mock
