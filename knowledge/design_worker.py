"""后台设计与代码生成任务处理器，支持应用内线程与独立命令。"""
import argparse
from contextlib import asynccontextmanager
import logging
import os
from threading import Event, Thread

from .code_generation import CodeGenerationService
from .db import database_url
from .design import DesignService


def _loop(url_provider, stop):
    while not stop.is_set():
        worked = False
        for service in (DesignService, CodeGenerationService):
            if stop.is_set():
                return
            try:
                worked = service(url_provider()).run_next() or worked
            except Exception as exc:
                logging.getLogger(__name__).warning('模型后台任务暂不可用：%s %s', service.__name__, type(exc).__name__)
        if not worked:
            stop.wait(2)


def design_lifespan(url_provider):
    @asynccontextmanager
    async def lifespan(app):
        stop, worker = Event(), None
        if os.environ.get('KNOWLEDGE_DESIGN_WORKER', 'enabled').lower() not in {'off', 'disabled', 'false', '0'}:
            worker = Thread(target=_loop, args=(url_provider, stop), name='knowledge-design-worker', daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker:
                worker.join(timeout=1)
    return lifespan


def main():
    parser = argparse.ArgumentParser(description='运行模型设计与代码生成后台任务')
    parser.add_argument('--once', action='store_true', help='处理至多一个排队任务后退出')
    args = parser.parse_args()
    if args.once:
        url = database_url()
        if not DesignService(url).run_next():
            CodeGenerationService(url).run_next()
    else:
        _loop(database_url, Event())


if __name__ == '__main__':
    main()
