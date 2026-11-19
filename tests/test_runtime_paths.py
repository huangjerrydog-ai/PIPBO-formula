import unittest
from pathlib import Path
from runtime_paths import data_directory

class RuntimePaths(unittest.TestCase):
    def test_source_retains_project_data(self):
        self.assertEqual(data_directory(Path('/project'),frozen=False,platform='win32',environ={}),Path('/project/data'))
    def test_windows_frozen_is_outside_bundle(self):
        self.assertEqual(data_directory(Path('/temporary-bundle'),frozen=True,platform='win32',environ={'LOCALAPPDATA':'/user/local'}),Path('/user/local/PIPBO-formula'))
    def test_explicit_data_directory(self):
        self.assertEqual(data_directory(Path('/bundle'),frozen=True,platform='win32',environ={'PIPBO_DATA':'/chosen-data'}),Path('/chosen-data').resolve())
