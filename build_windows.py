"""Run on Windows x64. Produces a portable directory ZIP, not an installer."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tomllib

ROOT=Path(__file__).resolve().parent

def main():
    if sys.platform!='win32':raise SystemExit('Windows build requires Windows. Run build_windows.bat or GitHub Actions.')
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--console',
        '--name','PIPBO-formula','--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build'),
        '--specpath',str(ROOT/'build'),'--paths',str(ROOT),'--hidden-import','server',
        '--collect-submodules','uvicorn','--collect-data','tzdata',
        '--add-data',str(ROOT/'static')+':static',str(ROOT/'main.py')],cwd=ROOT,check=True)
    folder=ROOT/'dist'/'PIPBO-formula'
    shutil.copyfile(ROOT/'WINDOWS.md',folder/'WINDOWS.md')
    shutil.copyfile(ROOT/'README.md',folder/'README.md')
    subprocess.run([sys.executable,str(ROOT/'smoke_windows.py'),str(folder/'PIPBO-formula.exe')],cwd=ROOT,check=True)
    version=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    release=ROOT/'release';release.mkdir(exist_ok=True)
    archive=Path(shutil.make_archive(str(release/f'PIPBO-formula-{version}-windows-x64'),'zip',root_dir=folder.parent,base_dir=folder.name))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    print('Windows package:',archive)

if __name__=='__main__':main()
