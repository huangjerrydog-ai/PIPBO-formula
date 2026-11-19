"""Keep packaged user data outside the disposable bundle directory."""
import os
import sys
from pathlib import Path


def data_directory(root, *, frozen=None, platform=None, environ=None):
    env=os.environ if environ is None else environ
    if env.get('PIPBO_DATA'):return Path(env['PIPBO_DATA']).expanduser().resolve()
    frozen=getattr(sys,'frozen',False) if frozen is None else frozen
    platform=sys.platform if platform is None else platform
    if frozen and platform=='win32':
        base=Path(env['LOCALAPPDATA']) if env.get('LOCALAPPDATA') else Path.home()/'AppData'/'Local'
        return base/'PIPBO-formula'
    return Path(root)/'data'
