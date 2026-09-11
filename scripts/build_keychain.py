"""Compile only the bundled local Keychain helper, outside the Vault."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory_service.safe_fs import SafeFS

p = argparse.ArgumentParser()
p.add_argument('--runtime', required=True)
args = p.parse_args()
fs = SafeFS(args.runtime, create=True)
fs.close()
target = Path(args.runtime) / 'keychain-helper'
if target.is_symlink():
    raise SystemExit('Unsafe helper path')
subprocess.run(['/usr/bin/swiftc', '-O', str(Path(__file__).with_name('keychain.swift')), '-o', str(target)], check=True)
os.chmod(target, 0o700)
print('Keychain helper compiled:', target)
