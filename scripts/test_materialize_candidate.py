# SPDX-License-Identifier: GPL-3.0-only
"""Small offline failure-path checks for the CI reconstruction tool."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).with_name('materialize_candidate.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MaterializationAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.overlay = self.repo / 'candidate-files'
        self.overlay.mkdir(parents=True)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.target = self.root / 'output'
        self.payload = {'app/source.txt': b'fixed source', 'runtime/library.bin': b'fixed native bytes',
                        'models/example.bin': b'fixed model bytes'}
        self.rows = [dict(path=k, sha256=sha(v), bytes=len(v)) for k, v in self.payload.items()]
        (self.overlay / 'app').mkdir()
        (self.overlay / 'app/source.txt').write_bytes(self.payload['app/source.txt'])
        wheel = self.payload['runtime/library.bin']
        (self.cache / sha(wheel)).write_bytes(wheel)
        archive_path = self.root / 'sample.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('upstream/example.bin', self.payload['models/example.bin'])
        archive = archive_path.read_bytes()
        (self.cache / sha(archive)).write_bytes(archive)
        self.plan = [dict(kind='file', target='runtime/library.bin', bytes=len(wheel), sha256=sha(wheel),
                          url='https://files.pythonhosted.org/never-used-offline'),
                     dict(kind='zip', members={'upstream/example.bin':'models/example.bin'},
                          bytes=len(archive), sha256=sha(archive),
                          url='https://github.com/never-used-offline')]
        self.write_metadata()

    def tearDown(self):
        self.temp.cleanup()

    def write_metadata(self):
        manifest = json.dumps(self.rows).encode()
        (self.overlay / 'payload-sha256.json').write_bytes(manifest)
        (self.repo / 'candidate-identity.json').write_text(json.dumps(
            dict(manifest_sha256=sha(manifest), expected_files=len(self.rows))))
        (self.repo / 'download-plan.json').write_text(json.dumps(self.plan))

    def run_builder(self):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), '--repository', str(self.repo),
                               '--target', str(self.target), '--cache', str(self.cache), '--offline'],
                              capture_output=True, text=True, timeout=15)

    def test_exact_frozen_bytes_reconstructed_without_network(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['native_windows_execution_tested'])
        self.assertFalse(report['original_zip_extraction_tested'])
        for name, expected in self.payload.items():
            self.assertEqual((self.target / name).read_bytes(), expected)

    def test_existing_destination_preserved(self):
        self.target.mkdir()
        (self.target / 'user-material').write_bytes(b'preserve')
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.target.iterdir()), [self.target / 'user-material'])
        self.assertEqual((self.target / 'user-material').read_bytes(), b'preserve')

    def test_corrupt_download_rejected_without_pass_report(self):
        (self.cache / self.plan[0]['sha256']).write_bytes(b'altered')
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"status": "passed"', result.stdout)

    def test_tampered_source_rejected_before_target_creation(self):
        (self.overlay / 'app/source.txt').write_bytes(b'changed')
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertFalse(self.target.exists())

    def test_path_escape_rejected_before_copy(self):
        self.plan[1]['members'] = {'../escape':'models/example.bin'}
        self.write_metadata()
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_tar_link_not_materialized(self):
        path = self.root / 'link.tar'
        with tarfile.open(path, 'w') as archive:
            info = tarfile.TarInfo('upstream/example.bin')
            info.type = tarfile.SYMTYPE
            info.linkname = '../../outside'
            archive.addfile(info)
        content = path.read_bytes()
        (self.cache / sha(content)).write_bytes(content)
        self.plan[1].update(kind='tar', bytes=len(content), sha256=sha(content))
        self.write_metadata()
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'models/example.bin').exists())


if __name__ == '__main__':
    unittest.main()
