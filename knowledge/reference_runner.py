"""生成并真实执行内置参考场景，不接受任意代码、命令或工作目录。

这是用于验证第四阶段流程的合成业务案例，不代表项目已经确认这些业务要求。
参考代码和独立测试均随应用发布；调用方只能选择明确允许的场景标识。
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from textwrap import dedent
import time

from .semantics import KnowledgeError


SCENARIO = "order_cancellation_v1"
RUN_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 16 * 1024
EXPECTED_TESTS = 4


_IMPLEMENTATION = dedent('''\
    """订单取消参考实现；仅为明确标记的合成场景。"""
    from dataclasses import dataclass


    class CancellationRejected(ValueError):
        """订单不满足取消条件。"""


    @dataclass
    class Order:
        payment_status: str = "unpaid"
        status: str = "open"


    def cancel_order(order: Order) -> bool:
        """未支付订单可取消；重复调用保持状态且不重复产生取消动作。

        首次取消返回 True，已取消的未支付订单返回 False。
        已支付及其他非未支付状态均不允许取消。
        """
        if order.payment_status != "unpaid":
            raise CancellationRejected("仅未支付订单允许取消")
        if order.status == "cancelled":
            return False
        order.status = "cancelled"
        return True
''')

_TESTS = dedent('''\
    """独立的参考验收测试；实际运行，不以文字声明替代执行。"""
    import unittest

    from order_service import CancellationRejected, Order, cancel_order


    class OrderCancellationTests(unittest.TestCase):
        def test_unpaid_order_can_be_cancelled(self):
            order = Order(payment_status="unpaid")
            self.assertTrue(cancel_order(order))
            self.assertEqual(order.status, "cancelled")

        def test_paid_order_is_rejected_without_mutation(self):
            order = Order(payment_status="paid")
            with self.assertRaises(CancellationRejected):
                cancel_order(order)
            self.assertEqual(order.status, "open")
            self.assertEqual(order.payment_status, "paid")

        def test_repeat_cancellation_is_idempotent(self):
            order = Order(payment_status="unpaid")
            self.assertTrue(cancel_order(order))
            self.assertFalse(cancel_order(order))
            self.assertFalse(cancel_order(order))
            self.assertEqual(order.status, "cancelled")

        def test_cancelling_one_order_does_not_change_another(self):
            first = Order(payment_status="unpaid")
            second = Order(payment_status="unpaid")
            cancel_order(first)
            self.assertEqual(first.status, "cancelled")
            self.assertEqual(second.status, "open")


    if __name__ == "__main__":
        unittest.main()
''')


def build_reference(scenario: str = SCENARIO) -> dict:
    """返回内置受信任代码、测试及可核对摘要，不写入调用方指定目录。"""
    if scenario != SCENARIO:
        raise KnowledgeError("不支持的参考场景，只允许 order_cancellation_v1")
    files = {"order_service.py": _IMPLEMENTATION, "test_order_service.py": _TESTS}
    encoded = json.dumps({"scenario": scenario, "files": files}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"scenario": scenario, "files": files,
            "sha256": {name: hashlib.sha256(content.encode("utf-8")).hexdigest() for name, content in files.items()},
            "bundle_sha256": hashlib.sha256(encoded).hexdigest(), "tests_expected": EXPECTED_TESTS,
            "note": "此内置订单取消场景是合成参考，不推定项目已批准对应业务规则。"}


def _output(stream):
    stream.flush()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    return stream.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace"), size > MAX_OUTPUT_BYTES


def run_reference(scenario: str = SCENARIO) -> dict:
    """在临时目录真实运行固定的 unittest 命令，返回有界日志与执行结果。

    该入口不能运行任意来源代码，不接受解释器、工作目录、命令或超时覆盖。
    隔离解释器选项用于排除用户路径和字节码输出，并不宣称是恶意代码沙箱。
    """
    build = build_reference(scenario)
    command = [sys.executable, "-I", "-B", "-X", "utf8", "-m", "unittest", "discover",
               "-s", ".", "-p", "test_order_service.py", "-v"]
    started = time.monotonic()
    outcome, returncode, executed, error = "failed", None, True, None
    with TemporaryDirectory(prefix="engineering-knowledge-reference-") as directory:
        workspace = Path(directory).resolve()
        for name, content in build["files"].items():
            # 文件名来自内置常量；不使用调用方路径、相对跳转或动态模板。
            (workspace / name).write_text(content, encoding="utf-8", newline="\n")
        with TemporaryFile() as stdout_file, TemporaryFile() as stderr_file:
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"PYTHONPATH", "PYTHONHOME"}}
            try:
                completed = subprocess.run(command, cwd=workspace, stdin=subprocess.DEVNULL,
                    stdout=stdout_file, stderr=stderr_file, timeout=RUN_TIMEOUT_SECONDS,
                    check=False, shell=False, close_fds=True, env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                returncode = completed.returncode
            except subprocess.TimeoutExpired:
                outcome, error = "timeout", "内置参考测试执行超时，子进程已终止。"
            except OSError as exc:
                executed, error = False, "无法启动本地参考测试：" + type(exc).__name__
            stdout, stdout_truncated = _output(stdout_file)
            stderr, stderr_truncated = _output(stderr_file)
    match = re.search(r"\bRan\s+(\d+)\s+tests?\s+in\b", stderr)
    tests_run = int(match.group(1)) if match else 0
    passed = (executed and returncode == 0 and tests_run >= EXPECTED_TESTS
              and re.search(r"^OK\s*$", stderr, re.MULTILINE) is not None)
    if passed:
        outcome = "passed"
    return {**build, "outcome": outcome, "passed": passed, "executed": executed,
            "command": command, "returncode": returncode, "stdout": stdout, "stderr": stderr,
            "tests_run": tests_run, "duration_ms": round((time.monotonic() - started) * 1000),
            "output_truncated": stdout_truncated or stderr_truncated,
            "error": error,
            "execution_note": "仅实际执行应用内置的受信任参考代码；不等于已验证任意项目源码或生产环境。"}
