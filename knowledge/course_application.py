"""单学期选课教学应用；SQLite 事务保护容量、幂等和学生课表。"""
import json
import sqlite3
from contextlib import contextmanager

RELEASE = "v1"


class EnrollmentError(ValueError):
    pass


class CourseApplication:
    def __init__(self, database, release=RELEASE):
        self.database, self.release = str(database), release
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS offering (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, teacher TEXT NOT NULL,
                  capacity INTEGER NOT NULL CHECK(capacity>=0), slots TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS enrollment (
                  id INTEGER PRIMARY KEY, student TEXT NOT NULL, offering TEXT NOT NULL REFERENCES offering(id),
                  UNIQUE(student,offering));
            ''')
            conn.executemany("INSERT OR IGNORE INTO offering VALUES (?,?,?,?,?)", [
                ('O101', '高等数学', '张老师', 1, json.dumps([{'weekday': 1, 'start_period': 1, 'end_period': 2}])),
                ('O102', '程序设计', '李老师', 2, json.dumps([{'weekday': 1, 'start_period': 2, 'end_period': 3}])),
                ('O103', '大学英语', '王老师', 2, json.dumps([{'weekday': 1, 'start_period': 3, 'end_period': 4}])),
                ('O104', '实验课程', '赵老师', 0, json.dumps([{'weekday': 2, 'start_period': 1, 'end_period': 2}]))])

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def conflicts(first, second):
        return any(a['weekday'] == b['weekday'] and a['start_period'] <= b['end_period']
                   and b['start_period'] <= a['end_period'] for a in first for b in second)

    def offerings(self):
        with self.connect() as conn:
            rows = conn.execute('''SELECT o.*,count(e.id) AS enrolled_count FROM offering o
                LEFT JOIN enrollment e ON e.offering=o.id GROUP BY o.id ORDER BY o.id''').fetchall()
            return [{**dict(row), 'slots': json.loads(row['slots'])} for row in rows]

    def enrollments(self, student):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('''SELECT e.id,e.offering,o.name FROM enrollment e
                JOIN offering o ON o.id=e.offering WHERE student=? ORDER BY e.id''', (student,))]

    def enroll(self, student, offering):
        if student not in ('S001', 'S002', 'S003'):
            raise EnrollmentError('请选择教学用预置学生')
        with self.connect() as conn:
            # 先获得数据库写事务，再一起检查并写入；重复请求在容量及冲突检查前处理。
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM offering WHERE id=?', (offering,)).fetchone()
            if not row:
                raise EnrollmentError('教学班不存在')
            existing = conn.execute('SELECT id FROM enrollment WHERE student=? AND offering=?', (student, offering)).fetchone()
            if existing:
                return {'id': existing['id'], 'created': False}
            if self.release == 'v1.2':
                old = conn.execute('''SELECT o.slots FROM enrollment e JOIN offering o ON o.id=e.offering
                    WHERE e.student=?''', (student,)).fetchall()
                if any(self.conflicts(json.loads(row['slots']), json.loads(item['slots'])) for item in old):
                    raise EnrollmentError('与已选教学班上课时间冲突')
            if self.release in ('v1.1', 'v1.2'):
                count = conn.execute('SELECT count(*) FROM enrollment WHERE offering=?', (offering,)).fetchone()[0]
                if count >= row['capacity']:
                    raise EnrollmentError('教学班已满，不能新增选课')
            cursor = conn.execute('INSERT INTO enrollment(student,offering) VALUES (?,?)', (student, offering))
            return {'id': cursor.lastrowid, 'created': True}
