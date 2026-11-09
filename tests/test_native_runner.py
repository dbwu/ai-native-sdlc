"""原生运行器区分编译、测试与工具错误，并且只执行固定命令。"""
from contextlib import nullcontext
from pathlib import PurePosixPath
import sys
from types import SimpleNamespace

import pytest

from knowledge.native_runner import native_runner


def runner(profile, paths, *, exit_code=lambda command: 0, missing=(), protect_report=True):
    namespace = {'__name__': 'runner_test'}
    exec(compile(native_runner(profile, 'report-test:'), '<runner>', 'exec'), namespace)

    root = PurePosixPath('/workspace')
    commands = []

    def run(command, **kwargs):
        assert kwargs['shell'] is False and kwargs['check'] is False
        assert kwargs['stdout'] is sys.stderr and kwargs['stderr'] is sys.stderr
        assert str(kwargs['cwd']).replace('\\', '/') == '/build/run'
        commands.append(command)
        return SimpleNamespace(returncode=exit_code(command))

    namespace.update(Path=PurePosixPath, protect_report_channel=lambda: protect_report,
                     shutil=SimpleNamespace(which=lambda name: None if name in missing else '/usr/bin/' + name),
                     subprocess=SimpleNamespace(run=run, DEVNULL=-3), TemporaryDirectory=lambda **kw: nullcontext('/build/run'),
                     source_files=lambda workspace, suffixes: sorted(root / path for path in paths if PurePosixPath(path).suffix in suffixes))
    status, report = namespace['run'](profile, root, PurePosixPath('/build'))
    return status, report, commands


def test_c_compiles_main_and_implementation_but_links_only_implementation_for_test(capsys):
    status, report, commands = runner('c_native', ['src/main.c', 'src/@logic.c', 'tests/test_behavior.c'])
    assert status == 0 and report['successful'] and report['tests_run'] == 1
    assert report['compile_status'] == report['test_status'] == 'passed'
    assert report['compile_returncode'] == report['test_returncode'] == 0
    assert len(commands) == 4
    implementation, main, link, test = commands
    assert '/workspace/src/@logic.c' in implementation and '/workspace/src/main.c' in main
    assert '/build/run/unit-0.o' in link and '/build/run/unit-1.o' not in link
    assert '-std=c11' in link and link[-1] == '-lm' and test == ['/build/run/test-0']
    captured = capsys.readouterr()
    assert captured.out == '' and '【编译】' in captured.err and '【测试】' in captured.err


def test_cpp_failed_test_keeps_compile_success_and_counts_all_executed_tests():
    status, report, commands = runner('cpp_native', ['src/logic.cc', 'tests/test_a.cpp', 'tests/test_b.cxx'],
                                     exit_code=lambda command: 17 if command[0] == '/build/run/test-0' else 0)
    assert status == 3 and report['compile_status'] == 'passed' and report['test_status'] == 'failed'
    assert report['tests_run'] == 2 and report['failures'] == 1 and report['test_returncode'] == 17
    assert commands[0][0] == '/usr/bin/g++' and '-std=c++17' in commands[0]
    assert commands[-1] == ['/build/run/test-1']


@pytest.mark.parametrize('profile,paths,executables', [
    ('c_native', ['logic.c', 'logic_test.c', 'tests/test_limit.c'], ['test-0', 'test-1']),
    ('java_native', ['Logic.java', 'LogicTest.java', 'src/test/java/course/LimitTest.java'], ['LogicTest', 'course.LimitTest']),
])
def test_existing_root_sources_and_tests_can_coexist_with_canonical_test_tree(profile, paths, executables):
    status, report, commands = runner(profile, paths)
    assert status == 0 and report['tests_run'] == 2
    assert [PurePosixPath(command[-1]).name for command in commands[-2:]] == executables
    assert any('/workspace/' + paths[0] in command for command in commands[:-2])


@pytest.mark.parametrize('compiler_code', [0, 23])
def test_java_uses_fixed_classpath_and_assertions_and_skips_tests_if_compile_failed(compiler_code):
    status, report, commands = runner('java_native', ['src/main/java/course/Logic.java', 'src/test/java/course/LogicTest.java',
                                                     'src/test/java/course/TestHelper.java'],
                                    exit_code=lambda command: compiler_code if command[0] == '/usr/bin/javac' else 0)
    assert commands[0][-3:] == ['/workspace/src/main/java/course/Logic.java', '/workspace/src/test/java/course/LogicTest.java',
                               '/workspace/src/test/java/course/TestHelper.java']
    assert '-proc:none' in commands[0] and '--release' in commands[0] and '17' in commands[0]
    if compiler_code:
        assert status == 2 and report['compile_status'] == 'failed' and report['compile_returncode'] == 23
        assert report['test_status'] == 'not_run' and report['tests_run'] == 0 and len(commands) == 1
    else:
        assert status == 0 and report['tests_run'] == 1 and len(commands) == 2
        assert commands[1][-4:] == ['-ea', '-cp', '/build/run', 'course.LogicTest']


@pytest.mark.parametrize('paths,compile_status,compiler_code', [(['src/logic.c'], 'passed', 0), ([], 'not_run', None)])
def test_no_test_entry_is_never_reported_as_success(paths, compile_status, compiler_code):
    status, report, commands = runner('c_native', paths)
    assert status == 4 and not report['successful'] and report['test_status'] == 'no_tests'
    assert report['tests_run'] == 0 and report['compile_status'] == compile_status
    assert report['compile_returncode'] == compiler_code and len(commands) == len(paths)


def test_missing_tool_is_an_environment_error_without_running_commands():
    status, report, commands = runner('java_native', ['LogicTest.java'], missing=('javac',))
    assert status == 5 and report['error'] == 'tool_missing' and report['compile_status'] == 'not_run'
    assert report['tests_run'] == 0 and commands == []


def test_native_commands_never_start_if_report_channel_protection_fails():
    status, report, commands = runner('c_native', ['test_logic.c'], protect_report=False)
    assert status == 5 and report['error'] == 'report_protection_failed'
    assert report['compile_status'] == report['test_status'] == 'not_run' and commands == []
