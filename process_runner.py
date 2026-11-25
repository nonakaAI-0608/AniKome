import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('キャンセルしました')


def stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if result.returncode and process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=15)


def stream_process(command, cwd, log, cancel=None, timeout=3600, encoding='utf-8', env=None):
    check_cancel(cancel)
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding=encoding, errors='replace', env=env, **kwargs)
    lines = queue.Queue(maxsize=2000)
    finished = threading.Event()
    def reader():
        try:
            for line in process.stdout:
                while not finished.is_set():
                    try:
                        lines.put(line.rstrip('\r\n'), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        finally:
            process.stdout.close()
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    recent = deque(maxlen=25)
    started = time.monotonic()
    try:
        while process.poll() is None or thread.is_alive() or not lines.empty():
            check_cancel(cancel)
            if time.monotonic() - started > timeout:
                raise TimeoutError('処理が制限時間を超えました。')
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line:
                recent.append(line)
                log(line)
        check_cancel(cancel)
        if process.wait():
            raise RuntimeError(f'終了コード {process.returncode}\n' + '\n'.join(recent)[-2000:])
    finally:
        finished.set()
        stop_tree(process)
        thread.join(timeout=2)
