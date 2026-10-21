"""File-operation checks: sidecars, archive preservation, collision and rerun."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1]/'run1.5_nisar_select_dhdh.py'
DATES = ['20260629','20260711','20260723','20260816','20260828','20260909','20260921']
KEPT = ['20260629','20260723','20260816','20260909']
SUFFIXES = ['.h5','.h5.iso.xml','_NATIVE.kml','_NATIVE_A_HH.png',
            '_QA_REPORT.pdf','_QA_STATS.h5','_QA_SUMMARY.csv','.rc.yaml']


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='run1_5_test_')
        self.data = Path(self.temp.name)/'data'
        self.data.mkdir()
        self.archive = self.data/'before_20260629'
        self.archive.mkdir()
        (self.archive/'old_DHDH_20251207.h5').write_bytes(b'keep old archive unchanged')
        self.products = {}
        for i,date in enumerate(DATES):
            mode,pol = ('4005','DHDH') if date in KEPT else ('2005','QPDH')
            stem = f'NISAR_L1_PR_RSLC_{i+24:03d}_048_D_074_{mode}_{pol}_A_{date}T125814_{date}T125849_P05023_N_F_J_001'
            files = []
            for suffix in SUFFIXES:
                path = self.data/(stem+suffix)
                path.write_bytes(('test content '+path.name).encode())
                files.append(path)
            self.products[date] = files
        (self.data/'download-all.py').write_text('# downloader stays here\n')

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *options):
        return subprocess.run([sys.executable,str(SCRIPT),*options,'--data',str(self.data)],
                              capture_output=True,text=True)

    def snapshot(self):
        return {str(p.relative_to(self.data)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.data.rglob('*') if p.is_file()}

    def test_preview_has_no_effects(self):
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('[KEEP] 4 dates; 32',result.stdout)
        self.assertIn('[ARCHIVE] 3 dates; 24',result.stdout)
        self.assertEqual(before,self.snapshot())
        self.assertFalse((self.data/'not_DHDH').exists())
        self.assertFalse((self.data/'.run1_5.lock').exists())

    def test_moves_all_sidecars_without_touching_existing_archive(self):
        before = self.snapshot()
        archived_stat = (self.archive/'old_DHDH_20251207.h5').stat()
        result = self.invoke('1')
        self.assertEqual(result.returncode,0,result.stderr)
        for date,files in self.products.items():
            for original in files:
                final = original if date in KEPT else self.data/'not_DHDH'/original.name
                self.assertTrue(final.is_file(),str(final))
                self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(),before[original.name])
                if date not in KEPT:
                    self.assertFalse(original.exists())
        self.assertEqual((self.archive/'old_DHDH_20251207.h5').stat().st_mtime_ns,archived_stat.st_mtime_ns)
        self.assertTrue((self.data/'download-all.py').is_file())
        self.assertEqual(len(list((self.data/'not_DHDH').glob('run1_5_moves_*.tsv'))),1)
        after = self.snapshot()
        again = self.invoke('1')
        self.assertEqual(again.returncode,0,again.stderr)
        self.assertIn('Already selected',again.stdout)
        self.assertEqual(after,self.snapshot())

    def test_collision_stops_before_first_move(self):
        target = self.data/'not_DHDH'
        target.mkdir()
        (target/self.products['20260828'][-1].name).write_bytes(b'existing destination')
        before = self.snapshot()
        result = self.invoke('1')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('destination already exists',result.stderr)
        self.assertEqual(before,self.snapshot())

    def test_archive_only_is_not_searched(self):
        for files in self.products.values():
            for source in files:
                source.rename(self.archive/source.name)
        before = self.snapshot()
        result = self.invoke('1')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('were not searched',result.stderr)
        self.assertEqual(before,self.snapshot())

    def test_archive_paths_cannot_escape(self):
        before = self.snapshot()
        result = self.invoke('1','--archive-name','../outside')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(before,self.snapshot())

    def test_add_to_requested_existing_archive_preserves_old_files(self):
        old = self.archive/'old_DHDH_20251207.h5'
        old_content,old_time = old.read_bytes(),old.stat().st_mtime_ns
        result = self.invoke('1','--archive-name','before_20260629')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(old.read_bytes(),old_content)
        self.assertEqual(old.stat().st_mtime_ns,old_time)
        for date,files in self.products.items():
            for original in files:
                destination = original if date in KEPT else self.archive/original.name
                self.assertTrue(destination.is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
