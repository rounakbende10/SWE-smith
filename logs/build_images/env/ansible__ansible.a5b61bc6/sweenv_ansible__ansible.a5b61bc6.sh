#!/bin/bash

git clone https://github.com/ansible/ansible.git
git checkout a5b61bc6
#!/bin/bash
set -e
conda create -n testbed python=3.12 -y
conda activate testbed || source activate testbed
pip install -e .
pip install pytest pytest-mock pexpect pywinrm passlib bcrypt
