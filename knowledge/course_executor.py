"""注册的选课教学适配器；实际执行绑定版本的源码，测试来自独立受信任套件。"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time

from .semantics import KnowledgeError

TEST_SOURCE = '''import tempfile
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from course_app import CourseApplication, EnrollmentError, RELEASE

class CourseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = CourseApplication(Path(self.temp.name) / "school.db")
    def test_directory(self):
        self.assertEqual(len(self.app.offerings()), 4)
    def test_enroll_and_query(self):
        self.assertTrue(self.app.enroll("S001", "O101")["created"])
        self.assertEqual(self.app.enrollments("S001")[0]["offering"], "O101")
    def test_idempotent(self):
        first = self.app.enroll("S001", "O101")
        self.assertEqual(self.app.enroll("S001", "O101")["id"], first["id"])
        self.assertEqual(len(self.app.enrollments("S001")), 1)
    def test_student_isolation(self):
        self.app.enroll("S001", "O101")
        self.assertEqual(self.app.enrollments("S002"), [])
    def test_invalid_offering(self):
        with self.assertRaises(EnrollmentError): self.app.enroll("S001", "missing")
'''
CAPACITY_TESTS = '''
    def test_capacity(self):
        self.app.enroll("S001", "O101")
        with self.assertRaises(EnrollmentError): self.app.enroll("S002", "O101")
        self.assertEqual(self.app.offerings()[0]["enrolled_count"], 1)
    def test_zero_capacity(self):
        with self.assertRaises(EnrollmentError): self.app.enroll("S001", "O104")
    def test_last_seat_concurrent(self):
        def choose(student):
            try: return self.app.enroll(student,"O101")["created"]
            except EnrollmentError: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(choose, ["S001", "S002"]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.app.offerings()[0]["enrolled_count"], 1)
'''
TIME_TESTS = '''
    def test_overlap(self):
        self.app.enroll("S001", "O101")
        with self.assertRaises(EnrollmentError): self.app.enroll("S001", "O102")
        self.assertEqual(len(self.app.enrollments("S001")), 1)
    def test_adjacent(self):
        self.app.enroll("S001", "O101")
        self.assertTrue(self.app.enroll("S001", "O103")["created"])
    def test_different_weekday(self):
        self.assertFalse(self.app.conflicts([{"weekday":1,"start_period":1,"end_period":2}],
            [{"weekday":2,"start_period":1,"end_period":2}]))
    def test_multiple_slots(self):
        with self.app.connect() as conn:
            conn.execute("UPDATE offering SET slots=? WHERE id='O103'", ('[{"weekday":2,"start_period":1,"end_period":2},{"weekday":1,"start_period":2,"end_period":4}]',))
        self.app.enroll("S001", "O101")
        with self.assertRaises(EnrollmentError): self.app.enroll("S001", "O103")
    def test_same_student_concurrent(self):
        def choose(offering):
            try: return self.app.enroll("S001",offering)["created"]
            except EnrollmentError: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(choose,["O101","O102"]))
        self.assertEqual(sum(results),1)
'''


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def build_course(release):
    if release not in ('v1', 'v1.1', 'v1.2'):
        raise KnowledgeError('不支持的选课教学版本')
    source = Path(__file__).with_name('course_application.py').read_text(encoding='utf-8').replace('RELEASE = "v1"', f'RELEASE = "{release}"', 1)
    tests = TEST_SOURCE + (CAPACITY_TESTS if release != 'v1' else '') + (TIME_TESTS if release == 'v1.2' else '')
    files = {'course_app.py': source, 'test_course_app.py': tests}
    return {'scenario': 'course_selection_v1', 'release': release, 'files': files,
            'sha256': {key: digest(value) for key, value in files.items()}, 'tests_expected': 5 + (3 if release != 'v1' else 0) + (5 if release == 'v1.2' else 0)}


def run_course(release, source):
    build = build_course(release)
    if source != build['files']['course_app.py']:
        raise KnowledgeError('绑定代码不匹配已注册选课执行器；请重新审阅或配置对应执行器', 409)
    command = [sys.executable, '-I', '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_course_app.py', '-v']
    start = time.monotonic()
    with TemporaryDirectory(prefix='knowledge-course-') as directory, TemporaryFile() as out, TemporaryFile() as err:
        # 写入绑定版本的实际文本；只有匹配注册版本的代码才允许在本地执行。
        Path(directory, 'course_app.py').write_text(source, encoding='utf-8')
        Path(directory, 'test_course_app.py').write_text(build['files']['test_course_app.py'], encoding='utf-8')
        executed, code, error = True, None, None
        try:
            result = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                timeout=15, shell=False, env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')},
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            code = result.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            executed, error = False, '执行失败：' + type(exc).__name__
        out.seek(0); err.seek(0)
        stdout, stderr = out.read(16385), err.read(16385)
    match = re.search(r'Ran (\d+) tests? in', stderr.decode('utf-8',errors='replace'))
    count = int(match.group(1)) if match else 0
    passed = executed and code == 0 and count == build['tests_expected'] and bool(re.search(rb'^OK\s*$', stderr, re.M))
    return {**build, 'executed': executed, 'passed': passed, 'outcome': 'passed' if passed else 'failed',
            'returncode': code, 'tests_run': count, 'command': command, 'stdout': stdout[:16384].decode('utf-8',errors='replace'),
            'stderr': stderr[:16384].decode('utf-8',errors='replace'), 'output_truncated': len(stdout)>16384 or len(stderr)>16384,
            'duration_ms': round((time.monotonic()-start)*1000), 'error': error}
