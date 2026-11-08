"""Capture code locations without logging exception values, requests or locals."""
from pathlib import Path
import json
import time
import traceback
from . import __version__
from .core import SafetyError


def describe_error(exc, folder):
    if isinstance(exc, SafetyError):
        return str(exc)
    frames = [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
              for f in traceback.extract_tb(exc.__traceback__)[-8:]]
    record = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'version': __version__,
              'exception_type': type(exc).__name__, 'frames': frames}
    try:
        path = Path(folder) / 'program-errors.jsonl'
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix('.previous.jsonl'))
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except OSError:
        pass
    location = f'{frames[-1]["file"]}:{frames[-1]["line"]}' if frames else '未知位置'
    return f'程序内部数据处理失败（{type(exc).__name__}，{location}）。已有保存进度保留；诊断见 data/program-errors.jsonl'
