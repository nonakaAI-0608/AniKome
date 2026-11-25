"""Expose nndownload's actual progress counters as flushed JSON events."""
import json
import sys
import threading
from pathlib import Path

PREFIX = '@@NICO_PROGRESS@@'


def main():
    source = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(source.parent))
    from nndownload import nndownload as downloader
    base = downloader.Progress
    output_lock = threading.RLock()

    class ReportingProgress(base):
        def __init__(self, *args, **kwargs):
            kwargs['disable'] = True
            super().__init__(*args, **kwargs)

        def report(self):
            with output_lock:
                tasks = list(self.tasks)
                payload = {'completed': sum(int(t.completed) for t in tasks),
                           'threads': [{'name': t.description, 'completed': int(t.completed),
                                        'total': t.total} for t in tasks]}
                print(PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)

        def add_task(self, *args, **kwargs):
            task = super().add_task(*args, **kwargs)
            self.report()
            return task

        def update(self, *args, **kwargs):
            result = super().update(*args, **kwargs)
            self.report()
            return result

        def advance(self, *args, **kwargs):
            result = super().advance(*args, **kwargs)
            self.report()
            return result

    downloader.Progress = ReportingProgress
    errors = []
    previous = threading.excepthook
    def thread_error(args):
        errors.append(str(args.exc_value))
        previous(args)
    threading.excepthook = thread_error
    sys.argv = [str(source)] + sys.argv[2:]
    try:
        downloader.cli()
    finally:
        if errors:
            print('コメント取得スレッドに失敗がありました: ' + '; '.join(errors), flush=True)
            raise SystemExit(1)


if __name__ == '__main__':
    main()
