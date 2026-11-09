"""在隔离容器内使用固定命令编译源文件并运行独立测试入口。"""

NATIVE_PROFILES = frozenset({'c_native', 'cpp_native', 'java_native'})

_RUNNER = r'''
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def log(phase, message):
    print(f'【{phase}】{message}', file=sys.stderr, flush=True)


def invoke(command, build):
    return subprocess.run(command, cwd=build, stdin=subprocess.DEVNULL, stdout=sys.stderr,
                          stderr=sys.stderr, shell=False, check=False).returncode


def protect_report_channel():
    # Linux 的 proc fd 权限按 ptrace 检查；关闭 dumpable 后同 UID 测试也不能打开报告流。
    pr_set_dumpable = 4
    try:
        prctl = ctypes.CDLL(None).prctl
        prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        return prctl(pr_set_dumpable, 0, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def source_files(root, suffixes):
    return sorted(path for path in root.rglob('*') if path.is_file() and path.suffix in suffixes)


def native_compile(workspace, build, compiler, standard, suffixes):
    test_root = workspace / 'tests'
    src_root = workspace / 'src'
    sources = source_files(workspace, suffixes)
    in_test_tree = [path for path in sources if path.is_relative_to(test_root)]
    tests = [path for path in in_test_tree if path.stem.startswith('test_')]
    tests.extend(path for path in sources if path not in in_test_tree and not path.is_relative_to(src_root)
                 and (path.stem.startswith('test_') or path.stem.endswith('_test')))
    helpers = [path for path in in_test_tree if path not in tests]
    production = [path for path in sources if path not in in_test_tree and path not in tests]
    if not (production or helpers or tests):
        return None, []

    flags = [compiler, f'-std={standard}', '-O0', '-g0', '-Wall', '-Wextra',
             '-fdiagnostics-color=never', '-pthread']
    for include in (workspace, workspace / 'include', src_root):
        flags.extend(['-I', str(include)])
    objects = []
    for index, source in enumerate(production + helpers):
        target = str(build / f'unit-{index}.o')
        log('编译', f'编译 {source.relative_to(workspace)}')
        code = invoke(flags + ['-c', str(source), '-o', target], build)
        if code:
            return code, []
        if source.stem != 'main' or source in helpers:
            objects.append(target)

    entries = []
    for index, source in enumerate(tests):
        label = str(source.relative_to(workspace))
        executable = str(build / f'test-{index}')
        log('编译', f'编译并链接 {label}')
        command = flags + [str(source), *objects, '-o', executable]
        if standard == 'c11':
            command.append('-lm')
        code = invoke(command, build)
        if code:
            return code, []
        entries.append((label, [executable]))
    return 0, entries


JVM_LIMITS = ('-XX:+UseSerialGC', '-XX:ActiveProcessorCount=1', '-Xms16m', '-Xmx128m')


def java_compile(workspace, build, compiler, runtime):
    production_root = workspace / 'src' / 'main' / 'java'
    test_root = workspace / 'src' / 'test' / 'java'
    sources = source_files(workspace, {'.java'})
    tests = [(path, test_root if path.is_relative_to(test_root) else workspace) for path in sources
             if not path.is_relative_to(production_root) and path.stem.endswith('Test')]

    if not sources:
        return None, []
    log('编译', f'编译 {len(sources)} 个 Java 源文件')
    flags = [compiler, *('-J' + flag for flag in JVM_LIMITS), '--release', '17', '-encoding', 'UTF-8',
             '-proc:none', '-cp', str(build), '-d', str(build)]
    code = invoke(flags + [str(path) for path in sources], build)
    if code:
        return code, []
    entries = []
    for path, root in tests:
        parts = path.relative_to(root).with_suffix('').parts
        # FQN 来自路径，首字符不能被 java 解释为选项。路径不符时明确报测试失败。
        if not all(part.replace('$', '_').isidentifier() for part in parts):
            entries.append((str(path.relative_to(workspace)), None))
        else:
            entries.append((str(path.relative_to(workspace)), [runtime, *JVM_LIMITS, '-ea', '-cp', str(build), '.'.join(parts)]))
    return 0, entries


def run(profile, workspace, build_root):
    report = {'profile': profile, 'compile_status': 'not_run', 'test_status': 'not_run',
              'compile_returncode': None, 'test_returncode': None, 'tests_run': 0,
              'failures': 0, 'errors': 0, 'skipped': 0, 'successful': False}
    if not protect_report_channel():
        log('环境', '无法保护测试报告流，未启动编译和测试')
        report['error'] = 'report_protection_failed'
        return 5, report
    tools = {'c_native': ('gcc',), 'cpp_native': ('g++',), 'java_native': ('javac', 'java')}[profile]
    resolved = [shutil.which(tool) for tool in tools]
    missing = [tool for tool, path in zip(tools, resolved) if path is None]
    if missing:
        log('环境', '执行镜像缺少工具：' + '、'.join(missing))
        report['error'] = 'tool_missing'
        return 5, report
    try:
        with TemporaryDirectory(prefix='knowledge-native-', dir=build_root) as directory:
            build = Path(directory)
            if profile == 'java_native':
                code, entries = java_compile(workspace, build, *resolved)
            elif profile == 'c_native':
                code, entries = native_compile(workspace, build, resolved[0], 'c11', {'.c'})
            else:
                code, entries = native_compile(workspace, build, resolved[0], 'c++17', {'.cpp', '.cc', '.cxx'})
            if code is not None:
                report.update(compile_status='failed' if code else 'passed', compile_returncode=code)
            if code:
                log('编译', f'编译失败（退出码 {code}）')
                return 2, report
            if not entries:
                log('测试', '没有找到符合约定的测试入口')
                report['test_status'] = 'no_tests'
                return 4, report
            log('编译', '编译通过')
            for label, command in entries:
                log('测试', f'运行 {label}')
                if command is None:
                    log('测试', 'Java 测试路径不能转换为合法的类名')
                    report['errors'] += 1
                    continue
                code = invoke(command, build)
                report['tests_run'] += 1
                if code:
                    report['failures'] += 1
                    if report['test_returncode'] is None:
                        report['test_returncode'] = code
                    log('测试', f'失败（退出码 {code}） · {label}')
                else:
                    log('测试', f'通过 · {label}')
    except OSError:
        log('环境', '无法建立构建目录或启动工具进程')
        report['error'] = 'launch_failed'
        return 5, report

    if report['failures'] or report['errors']:
        report['test_status'] = 'failed'
        return 3, report
    report.update(test_status='passed', test_returncode=0, successful=True)
    return 0, report


if __name__ == '__main__':
    status, report = run(PROFILE, Path('/workspace'), Path('/build'))
    # 子进程 stdout/stderr 均转发至日志流，这个流只写固定运行器报告。
    print(REPORT_PREFIX + json.dumps(report), flush=True)
    raise SystemExit(status)
'''


def native_runner(profile: str, report_prefix: str) -> str:
    if profile not in NATIVE_PROFILES:
        raise ValueError('未知的原生代码隔离执行方式')
    return f'PROFILE = {profile!r}\nREPORT_PREFIX = {report_prefix!r}\n' + _RUNNER
