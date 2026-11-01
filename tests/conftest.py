"""
Session setup shared by every test.

This file is imported before any test module, and it has to run before anything imports Ansible:
Ansible reads its configuration once, at import time.

It makes sure every test runs against the code in this repository and nothing else. This machine
has other installed copies of this collection (under ~/.ansible, and in each playbook's
.ansible/ directory) that Ansible would otherwise prefer. So the only collection Ansible can see is a
symlink to this checkout, and no test can reach the real password store.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _isolate_ansible():
    """Build a collections root holding only this checkout, and point Ansible at it."""
    root = Path(tempfile.mkdtemp(prefix="kriansa-commons-tests-"))
    namespace_dir = root / "ansible_collections" / "kriansa"
    namespace_dir.mkdir(parents=True)
    (namespace_dir / "commons").symlink_to(REPO_ROOT)
    atexit.register(shutil.rmtree, root, ignore_errors=True)

    ansible_home = Path(tempfile.mkdtemp(prefix="kriansa-commons-ansible-home-"))
    atexit.register(shutil.rmtree, ansible_home, ignore_errors=True)

    os.environ.update(
        {
            "ANSIBLE_COLLECTIONS_PATH": str(root),
            # A pip-installed `ansible` package ships its own ansible_collections on sys.path
            "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False",
            "ANSIBLE_CONFIG": str(REPO_ROOT / "tests" / "ansible.cfg"),
            "ANSIBLE_HOME": str(ansible_home),
            "PASSWORD_STORE_DIR": "/nonexistent",
        }
    )
    return root


COLLECTIONS_ROOT = _isolate_ansible()

# pylint: disable-next=wrong-import-position
from ansible.plugins.loader import init_plugin_loader

init_plugin_loader()
