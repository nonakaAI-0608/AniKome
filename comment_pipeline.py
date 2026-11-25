"""Download comments, preserve original dates in XML, and match local episodes."""
import difflib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from process_runner import check_cancel, stream_process
from datetime import datetime
from pathlib import Path

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.ts', '.avi', '.mov', '.wmv', '.m2ts', '.webm'}


def clean(value):
    return ''.join(c for c in str(value) if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def json_to_xml(source, destination, cancel=None, progress=None):
    data = json.loads(Path(source).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('threads'), list):
        raise ValueError('nndownload形式のJSONではありません（threadsがありません）。')
    total = sum(len(t.get('comments', [])) for t in data['threads'])
    packet = ET.Element('packet')
    count = 0
    for thread in data['threads']:
        for comment in thread.get('comments', []):
            check_cancel(cancel)
            posted = datetime.fromisoformat(comment['postedAt'].replace('Z', '+00:00'))
            if posted.tzinfo is None:
                raise ValueError('postedAtにタイムゾーンがありません。')
            chat = ET.SubElement(packet, 'chat', {
                'thread': clean(thread.get('id', '')),
                'no': str(comment.get('no', count + 1)),
                'vpos': str(int(comment['vposMs']) // 10),
                'date': str(int(posted.timestamp())),
                'mail': clean(' '.join(comment.get('commands') or [])),
                'user_id': clean(comment.get('userId', '')),
                'premium': '3' if thread.get('fork') == 'owner' else '1' if comment.get('isPremium') else '0',
            })
            chat.text = clean(comment.get('body', ''))
            count += 1
            if progress and (count % 500 == 0 or count == total):
                progress(f'XML変換中: {count:,}/{total:,}件')
    if not count:
        raise ValueError('JSONにコメントがありません。ログイン状態・取得ログを確認してください。')
    ET.indent(packet, space='  ')
    ET.ElementTree(packet).write(destination, encoding='utf-8', xml_declaration=True)
    return count


def normalize_title(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = re.sub(r'^\[[ぁ-ゖ]\]\s*', '', value)
    value = re.sub(r'[（(]\s*dアニメストア[^)）]*[)）]', '', value)
    return ''.join(c for c in value if c.isalnum())


KANJI_DIGITS = {'〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
NUMBER = r'(\d+|[〇零一二三四五六七八九十百]+)'
# 「第1期」「第2部」などは話数ではないので、第N の直後がこれらの文字なら話数とみなさない。
NOT_EPISODE_UNIT = '期部弾巻季シ'
SPECIAL_RE = re.compile(r'OVA|OAD|番外|特別|総集編|劇場|映画|予告|メニュー|映像特典'
                        r'|\b(?:NC|NCOP|NCED|OP|ED|PV|CM|Menu|Preview|Trailer)\b', re.I)
# 「ひだまりスケッチ×SP」のように作品名に SP が含まれることがあるため、SP は話数の直後だけで判定する。
SPECIAL_AFTER_RE = re.compile(SPECIAL_RE.pattern + r'|\b(?:SP|Special)\b', re.I)
# 特典・ノンクレジット映像などを入れる汎用フォルダ名は作品名の別名にしない。
GENERIC_FOLDERS = {'nc', 'sp', 'sps', 'special', 'specials', 'extra', 'extras', 'omake', 'bonus',
                   'menu', 'cm', 'pv', 'ova', 'oad', 'bd', 'dvd', '特典', '映像特典'}


def parse_number(text):
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for char in text:
        if char == '百':
            total += (current or 1) * 100
            current = 0
        elif char == '十':
            total += (current or 1) * 10
            current = 0
        else:
            current = current * 10 + KANJI_DIGITS[char]
    return total + current


def clean_file_stem(stem):
    """Remove release-group tags and encode details that are not part of the title."""
    value = unicodedata.normalize('NFKC', stem).replace('_', ' ')
    value = re.sub(r'^(?:\s*[\[(【][^\[\]()【】]*[\])】])+\s*', '', value)
    value = re.sub(r'(?:\s*[\[(【][^\[\]()【】]*[\])】])+\s*$', '', value)
    return value.strip() or stem


def strict_title(value):
    """記号を残した比較用の作品名（「ご注文はうさぎですか？」と「？？」を区別する）。"""
    value = unicodedata.normalize('NFKC', value).casefold()
    value = re.sub(r'^\[[ぁ-ゖ]\]\s*', '', value)
    value = re.sub(r'[（(]\s*dアニメストア[^)）]*[)）]', '', value)
    return re.sub(r'\s', '', value)


def episode_key(value):
    parts = episode_parts(value)
    return (normalize_title(parts[0]), parts[1]) if parts else None


def episode_parts(value):
    """Only explicit episode markers; do not guess seasons or sequential order."""
    value = unicodedata.normalize('NFKC', value)
    patterns = [
        # 第1話・第1羽・第1R・第一話など、第N の後に単位が1文字続く表記。
        # 単位は文字（記号・数字は不可）なので「第11.5話」は話数として扱わない。
        r'^(.*?)\s*第\s*' + NUMBER + r'\s*(?![' + NOT_EPISODE_UNIT + r'])[^\W\d_]',
        r'^(.*?)\s*#\s*(\d+)\b',
        # 「Step1」「Lesson.1」「Chapter 1」など英単語＋番号の話数表記。
        # Season・Vol・Disc は話数ではないので含めない。
        r'^(.*?)\s+(?:episode|ep|act|page|stage|step|lesson|chapter|track|file|round|scene|story'
        r'|level|lv|phase|mission|report|trip|lap|karte)\.?\s*(\d+)\b',
        r'^(.*?)\s+-\s+(\d+)(?:\s|\[|$)',
        # 「作品名 01「サブタイトル」」のように話数だけが置かれた表記。
        r'^(.*?\S)\s+(\d{1,3})(?=\s|[「『\[]|$)',
    ]
    for number, pattern in enumerate(patterns):
        match = re.search(pattern, value, re.I)
        if match:
            prefix = match.group(1)
            # 「Season 2」「Vol.01」の数字は話数ではない。
            if number == len(patterns) - 1 and re.search(r'(?:season|s|vol\.?|part|cour|期|部)\s*$', prefix, re.I):
                continue
            remainder = value[match.end():]
            # Specials and multi-episode files require explicit user selection.
            if SPECIAL_RE.search(prefix) or SPECIAL_AFTER_RE.match(remainder.strip()):
                return None
            if re.match(r'\s*(?:[/／+＋&＆～~〜-]|・)\s*(?:第)?\s*\d+', remainder):
                return None
            if normalize_title(prefix):
                return prefix, parse_number(match.group(2))
    return None


class VideoIndex(dict):
    """(作品名, 話数) → 動画。ファイル名由来の作品名に加え、フォルダ名由来の別名も持つ。"""

    def __init__(self):
        super().__init__()
        self.aliases = {}

    def entries(self):
        for table in (self, self.aliases):
            for key, paths in table.items():
                for path in paths:
                    yield key, path


def folder_aliases(path, root):
    names = []
    folder = path.parent
    while True:
        title = normalize_title(folder.name)
        if title and title not in GENERIC_FOLDERS and title not in names:
            names.append(title)
        if folder == root or folder.parent == folder:
            break
        folder = folder.parent
    return names


def video_index(folder, cancel=None):
    root = Path(folder)
    if not root.is_dir():
        raise ValueError('照合する動画フォルダを指定してください。')
    index = VideoIndex()
    for path in root.rglob('*'):
        check_cancel(cancel)
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            key = episode_key(clean_file_stem(path.stem))
            if not key:
                continue
            index.setdefault(key, []).append(path)
            for alias in folder_aliases(path, root):
                if alias != key[0]:
                    index.aliases.setdefault((alias, key[1]), []).append(path)
    return index


def unique(paths):
    return list({path: None for path in paths})


def similarity(a, b):
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) * 0.5 + 0.5
    return difflib.SequenceMatcher(None, a, b).ratio()


# 照合の結果の種類。'exact' と 'folder' はそのまま保存対象、それ以外は確認が必要。
CONFIRMED_KINDS = ('exact', 'folder', 'manual')


def match_video(title, index):
    """Return (path, kind, note). kind: exact / folder / episode / similar / None."""
    key = episode_key(title)
    if not key:
        return None, None, '話数を読み取れません'
    exact = unique(index.get(key, []))
    if len(exact) == 1:
        return exact[0], 'exact', ''
    if len(exact) > 1:
        # 記号だけが違う別シーズンは、記号を残した作品名で絞り込む。
        wanted = strict_title(episode_parts(title)[0])
        strict = [path for path in exact if strict_title(episode_parts(clean_file_stem(path.stem))[0]) == wanted]
        if len(strict) == 1:
            return strict[0], 'exact', '記号まで一致'
        return None, None, f'同名候補が{len(exact)}件'
    alias = unique(getattr(index, 'aliases', {}).get(key, []))
    if len(alias) == 1:
        return alias[0], 'folder', 'フォルダ名で一致'
    if len(alias) > 1:
        return None, None, f'フォルダ名の候補が{len(alias)}件'
    same_episode = {}
    for (name, number), path in index.entries() if hasattr(index, 'entries') else ():
        if number == key[1]:
            same_episode[path] = max(same_episode.get(path, 0), similarity(key[0], name))
    if len(same_episode) == 1:
        return next(iter(same_episode)), 'episode', '話数のみ一致'
    ranked = sorted(same_episode.items(), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] >= 0.5 and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= 0.15):
        return ranked[0][0], 'similar', f'類似度 {ranked[0][1]:.2f}'
    if ranked:
        return None, None, f'第{key[1]}話の候補が{len(ranked)}件'
    return None, None, f'第{key[1]}話の動画なし'


def publish(source, destination):
    """Exclusive creation even if another process creates the output after preview."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open('xb') as output:
            created = True
            with Path(source).open('rb') as incoming:
                shutil.copyfileobj(incoming, output)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination


def download_comments(video_id, settings, folder, log, cancel=None, progress=None):
    script = Path(settings.get('nndownload', '')).expanduser().resolve()
    if script.is_dir():
        script = script / 'nndownload.py'
    requirements = script.parent / 'requirements.txt'
    if not script.is_file() or script.name.lower() != 'nndownload.py' or not requirements.is_file():
        raise ValueError('展開したnndownload-master直下のnndownload.pyを指定してください（requirements.txtと同じ場所）。')
    uv = shutil.which('uv')
    if not uv:
        raise ValueError('uvが見つかりません。コメント取得に成功したときと同じ環境で起動してください。')
    command = [uv, 'run', '--no-project', '--python', '3.11', '--with-requirements', str(requirements),
               'python', '-u', str(Path(__file__).with_name('download_bridge.py')), str(script), '--skip-media', '--download-comments',
               '--cookies-from-browser', 'firefox']
    count = int(settings['max_comments'])
    command += ['--all-comments'] if count == 0 else ['--comments-limit', str(count)]
    command += ['-o', str(Path(folder) / '{id}.{ext}'), 'https://www.nicovideo.jp/watch/' + video_id]
    log(f'{video_id}: コメント取得開始（Firefox、' + ('全件要求' if count == 0 else f'各スレッド最大{count}件') + '）')
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    # Remove the launcher's virtual environment hint; uv manages this command independently.
    env.pop('VIRTUAL_ENV', None)
    if progress:
        progress('コメント取得の準備中（Firefox認証・動画情報）')
    def receive(line):
        marker = '@@NICO_PROGRESS@@'
        if line.startswith(marker):
            try:
                state = json.loads(line[len(marker):])
                details = ' / '.join(f"{t['name']}: {t['completed']:,}" + (f"/{int(t['total']):,}" if t['total'] is not None else '') for t in state['threads'])
                text = f"コメント取得中: {state['completed']:,}件受信済み（{details}）"
                if progress: progress(text)
                log(text)
                return
            except (ValueError, KeyError, TypeError):
                pass
        log(line)
    stream_process(command, script.parent, receive, cancel, env=env)
    candidates = list(Path(folder).glob('*.comments.json'))
    if not candidates:
        candidates = list(Path(folder).glob('*.json'))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict) and isinstance(data.get('threads'), list):
                return path
        except (OSError, ValueError):
            pass
    raise RuntimeError('コメントJSONが出力されませんでした。取得ログを確認してください。')
