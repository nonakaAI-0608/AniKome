import json
from process_runner import Cancelled, check_cancel, stream_process
from comment_pipeline import download_comments, json_to_xml, video_index, match_video, publish, VIDEO_EXTENSIONS, CONFIRMED_KINDS
import os
import re
import shutil
import subprocess
import threading
import tempfile
import html
import xml.etree.ElementTree as ET
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except ImportError:
    TkinterDnD = None
    DND_FILES = None
import urllib.parse
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP = Path(__file__).resolve().parent
CONFIG = APP / 'settings.json'
API = 'https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search'
DANIME_CHANNEL_ID = '2632720'
HEADERS = {'User-Agent': 'NicoAssTool/1.0 (personal desktop application)'}
ID_RE = re.compile(r'^(?:sm|nm|so|ax|ca|yo|za|lv)\d+$', re.I)


def search_video(word, start=0):
    params = {'q': word, 'targets': 'title,description,tags', 'fields': 'contentId,title,description,viewCounter,commentCounter,channelId', '_sort': '-viewCounter', '_offset': start, '_limit': 50, '_context': 'nico-ass-tool'}
    req = urllib.request.Request(API + '?' + urllib.parse.urlencode(params), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as response:
        data = json.load(response)
    if data.get('meta', {}).get('status') != 200:
        raise RuntimeError(str(data.get('meta', '検索に失敗しました')))
    return data.get('data', [])


def search_with_danime(word, start=0, max_extra_pages=20):
    """Collect ordinary results plus channel matches beyond the first page."""
    first = search_video(word, start)
    matches = []
    seen = {row.get('contentId') for row in first}
    scanned = len(first)
    for page in range(1, max_extra_pages + 1):
        if len(first) < 50:
            break
        rows = search_video(word, start + page * 50)
        scanned += len(rows)
        for row in rows:
            if str(row.get('channelId', '')).removeprefix('ch') == DANIME_CHANNEL_ID and row.get('contentId') not in seen:
                matches.append(row)
                seen.add(row.get('contentId'))
        if len(rows) < 50 or len(matches) >= 50:
            break
    return first + matches, scanned


def extract_id(value):
    value = value.strip()
    if ID_RE.fullmatch(value):
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.hostname not in ('www.nicovideo.jp', 'nicovideo.jp'):
        raise ValueError('ニコニコ動画の動画URLか動画IDを入力してください')
    match = re.search(r'/watch/((?:sm|nm|so|ax|ca|yo|za)\d+)', parsed.path, re.I)
    if not match:
        raise ValueError('動画IDをURLから読み取れませんでした')
    return match.group(1)


def snapshot(folder, extension):
    return {str(p.resolve()): (p.stat().st_mtime_ns, p.stat().st_size) for p in folder.rglob('*' + extension) if p.is_file()}


def changed(folder, extension, before):
    files = [p for p in folder.rglob('*' + extension) if p.is_file() and before.get(str(p.resolve())) != (p.stat().st_mtime_ns, p.stat().st_size)]
    return sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def run(command, cwd, log, cancel=None):
    log('実行: ' + ' '.join(map(str, command)))
    stream_process(command, cwd, log, cancel, timeout=600, encoding='cp932')


def prepare_video_xml(source, destination, max_comments=0):
    # NicoConvAss の「ニコニコ動画コメント vpos→date変換」と同じ目的を
    # 変換前に確定させる。通常動画では投稿日時ではなく再生位置を使う。
    tree = ET.parse(source)
    root = tree.getroot()
    chats = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'chat']
    if not chats:
        raise ValueError(f'コメントが0件です: {source}\nXMLの先頭要素: <{root.tag}>。さきゅばすの取得ログと、このXMLの内容を確認してください。')
    if max_comments:
        # 新しい投稿から指定件数を残す。元 XML は変更しない。
        selected = {id(chat) for chat in sorted(chats, key=lambda c: int(c.get('date', '0') or '0'), reverse=True)[:max_comments]}
        for chat in chats:
            if id(chat) not in selected:
                root.remove(chat)
        chats = [chat for chat in chats if id(chat) in selected]
    for no, chat in enumerate(chats, 1):
        vpos = int(chat.get('vpos', '0'))
        chat.set('date', str(1700000000 + max(0, vpos) // 100))
        chat.set('no', chat.get('no') or str(no))
        chat.set('mail', chat.get('mail') or '')
    chats.sort(key=lambda c: int(c.get('vpos', '0')))
    if all(chat in list(root) for chat in chats):
        for chat in chats:
            root.remove(chat)
            root.append(chat)
    tree.write(destination, encoding='utf-8', xml_declaration=True)


def adjust_ass(path, resolution, semi):
    """Apply output settings to styles and dialogue override tags."""
    width, height = (1920, 1080) if resolution == '1080p' else (1280, 720)
    raw = path.read_bytes()
    try:
        content = raw.decode('utf-8-sig')
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        content = raw.decode('cp932')
        encoding = 'cp932'
    old_w = re.search(r'(?im)^PlayResX:\s*(\d+)', content)
    old_h = re.search(r'(?im)^PlayResY:\s*(\d+)', content)
    if not old_w or not old_h or not int(old_w.group(1)) or not int(old_h.group(1)):
        raise ValueError('ASS に有効な PlayResX / PlayResY がありません')
    sx, sy = width / int(old_w.group(1)), height / int(old_h.group(1))
    scale = (sx + sy) / 2
    alpha = '64' if semi else '00'
    content = re.sub(r'(?im)^PlayResX:\s*\d+', 'PlayResX: ' + str(width), content)
    content = re.sub(r'(?im)^PlayResY:\s*\d+', 'PlayResY: ' + str(height), content)

    def number(value, ratio):
        result = float(value) * ratio
        return str(round(result, 2)).rstrip('0').rstrip('.') if '.' in str(round(result, 2)) else str(round(result))

    def color(value):
        return re.sub(r'(?i)^&H[0-9a-f]{2}', '&H' + alpha, value.strip())

    def override(block):
        body = block[1:-1]
        if sx != 1 or sy != 1:
            # Coordinates of pos, move and org are absolute PlayRes coordinates.
            def coords(match):
                tag, values = match.group(1), match.group(2).split(',')
                for j in range(min(len(values), 4 if tag.lower() == 'move' else 2)):
                    values[j] = number(values[j].strip(), sx if j % 2 == 0 else sy)
                return '\\' + tag + '(' + ','.join(values) + ')'
            body = re.sub(r'\\(pos|move|org)\(([-\d., ]+)\)', coords, body, flags=re.I)
            def numeric(match):
                return match.group(1) + number(match.group(2), scale)
            body = re.sub(r'(\\(?:fs|bord|shad|blur|be))(-?\d+(?:\.\d+)?)', numeric, body, flags=re.I)
        body = re.sub(r'\\(?:alpha|[1-4]a)&H[0-9a-f]{2}&', '', body, flags=re.I)
        return '{' + body + '\\alpha&H' + alpha + '&}'

    lines = content.splitlines(keepends=True)
    section = ''
    style_format = []
    event_format = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped.lower()
        elif stripped.startswith('Format:'):
            fields = [field.strip().lower() for field in stripped[7:].split(',')]
            if section in ('[v4+ styles]', '[v4 styles]'):
                style_format = fields
            elif section == '[events]':
                event_format = fields
        elif section in ('[v4+ styles]', '[v4 styles]') and stripped.startswith('Style:'):
            fields = line[6:].rstrip('\r\n').split(',')
            for j, field in enumerate(style_format[:len(fields)]):
                if field in ('primarycolour', 'secondarycolour', 'outlinecolour', 'backcolour'):
                    fields[j] = color(fields[j])
                elif field in ('fontsize', 'outline', 'shadow', 'spacing', 'marginl', 'marginr') and scale != 1:
                    if re.fullmatch(r'\s*-?\d+(?:\.\d+)?\s*', fields[j]):
                        fields[j] = number(fields[j].strip(), sx if field in ('marginl', 'marginr') else scale)
                elif field == 'marginv' and sy != 1:
                    if re.fullmatch(r'\s*-?\d+(?:\.\d+)?\s*', fields[j]):
                        fields[j] = number(fields[j].strip(), sy)
            lines[i] = 'Style:' + ','.join(fields) + ('\r\n' if line.endswith('\r\n') else '\n')
        elif section == '[events]' and (stripped.startswith('Dialogue:') or stripped.startswith('Comment:')) and 'text' in event_format:
            prefix, payload = line.split(':', 1)
            fields = payload.rstrip('\r\n').split(',', len(event_format) - 1)
            if len(fields) == len(event_format):
                j = event_format.index('text')
                text = re.sub(r'\{[^{}]*\}', lambda match: override(match.group(0)), fields[j])
                # New override at start also covers lines with no existing tags.
                fields[j] = '{\\alpha&H' + alpha + '&}' + text
                lines[i] = prefix + ':' + ','.join(fields) + ('\r\n' if line.endswith('\r\n') else '\n')
    path.write_bytes(''.join(lines).encode(encoding))


def convert(video_id, title, settings, log, target_video, cancel=None, progress=None):
    progress = progress or (lambda text: None)
    check_cancel(cancel)
    video = Path(target_video).resolve()
    root = Path(settings['destination']).expanduser().resolve()
    if not video.is_relative_to(root):
        raise ValueError('保存・参照フォルダ内の動画を選んでください。')
    if not video.is_file():
        raise FileNotFoundError('参照する動画がありません: ' + str(video))
    target = video.with_suffix('.ass')
    if target.exists():
        raise FileExistsError('同名ASSがあるためスキップ: ' + str(target))
    converter = Path(settings['nicoconvass']).expanduser().resolve()
    if not converter.is_file() or converter.suffix.lower() != '.exe':
        raise ValueError('NicoConvAss.exeを指定してください。')
    (APP / 'work').mkdir(exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix=video_id + '_', dir=APP / 'work'))
    def job_log(message):
        with (job / 'process.log').open('a', encoding='utf-8') as stream:
            stream.write(message + '\n')
        log(message)
    try:
        source = download_comments(video_id, settings, job, job_log, cancel, progress)
        check_cancel(cancel)
        xml = job / (video_id + '.xml')
        progress('JSON → XML 変換中')
        count = json_to_xml(source, xml, cancel, progress)
        job_log(f'XML変換完了: {count:,}件')
        prepared = job / (job.name + '_ass.xml')
        check_cancel(cancel)
        progress('ASS用の再生時刻を準備中')
        prepare_video_xml(xml, prepared, 0)
        before = snapshot(converter.parent, '.ass')
        check_cancel(cancel)
        progress('NicoConvAssでASSに変換中')
        run([str(converter), str(prepared)], converter.parent, job_log, cancel)
        check_cancel(cancel)
        candidates = list(job.glob('*.ass')) + changed(converter.parent, '.ass', before)
        candidates = list({p.resolve(): p for p in candidates if prepared.stem in p.stem}.values())
        if len(candidates) != 1:
            raise RuntimeError('今回のASS出力を一意に検出できません。NicoConvAssの出力先を実行ファイルのフォルダまたは入力XMLのフォルダに設定してください。')
        if not candidates[0].stat().st_size:
            raise ValueError('出力されたASSが空です。')
        staged = job / 'adjusted.ass'
        shutil.copy2(candidates[0], staged)
        progress('ASSの解像度・透明度を適用中')
        adjust_ass(staged, settings['resolution'], settings['semi'])
        check_cancel(cancel)
        progress('動画名でASSを保存中')
        publish(staged, target)
        # Delete only this job's XML after successful ASS publication.
        for temporary_xml in job.glob('*.xml'):
            temporary_xml.unlink(missing_ok=True)
        job_log('ASS保存完了・作業用XML削除: ' + str(target))
        progress('保存完了')
        return target
    except Cancelled:
        job_log('キャンセル: ' + str(job))
        raise
    except Exception as error:
        job_log('失敗: ' + str(error))
        log('途中ファイル・ログ: ' + str(job))
        raise


class App(TkinterDnD.Tk if TkinterDnD else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ニコニコ コメント保存 v5 — ASS・リネーム')
        self.geometry('1060x830')
        self.rows = {}
        self.offset = 0
        self.busy = False
        self.cancel_event = threading.Event()
        try:
            config = json.loads(CONFIG.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            config = {}
        if config.get('video_folder'):
            config['destination'] = config['video_folder']
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        settings = ttk.LabelFrame(frame, text='初期設定', padding=8)
        settings.pack(fill='x')
        self.paths = {}
        for index, (key, label, default, kind) in enumerate((
            ('nndownload', 'nndownload.py（展開先の直下）', '', 'file'),
            ('nicoconvass', 'NicoConvAss.exe', '', 'file'),
            ('destination', 'ASS保存先・動画名の参照フォルダ', config.get('video_folder') or str(Path.home() / 'Downloads'), 'directory'),
        )):
            ttk.Label(settings, text=label).grid(row=index, column=0, sticky='w', padx=4, pady=3)
            var = tk.StringVar(value=config.get(key, default))
            self.paths[key] = var
            ttk.Entry(settings, textvariable=var).grid(row=index, column=1, sticky='ew', padx=4)
            ttk.Button(settings, text='参照', command=lambda v=var, k=kind: self.pick(v, k)).grid(row=index, column=2, padx=4)
        settings.columnconfigure(1, weight=1)
        self.only_so = tk.BooleanVar(value=config.get('only_so', False))
        self.include_danime = tk.BooleanVar(value=config.get('include_danime', False))
        self.max_comments = tk.StringVar(value=str(config.get('download_limit', '1000')))
        self.resolution = tk.StringVar(value=config.get('resolution', '1080p'))
        self.semi = tk.BooleanVar(value=config.get('semi', False))
        options = ttk.Frame(frame)
        options.pack(fill='x', pady=(7, 0))
        ttk.Checkbutton(options, text='公式チャンネルのみ（so動画）', variable=self.only_so, command=self.apply_filter).pack(side='left')
        ttk.Checkbutton(options, text='dアニメストア ニコニコ支店を含める', variable=self.include_danime, command=self.refresh_search).pack(side='left', padx=(12, 0))
        ttk.Label(options, text='出力解像度:').pack(side='left', padx=(18, 3))
        ttk.Radiobutton(options, text='720p', value='720p', variable=self.resolution).pack(side='left')
        ttk.Radiobutton(options, text='1080p', value='1080p', variable=self.resolution).pack(side='left')
        ttk.Checkbutton(options, text='半透明（透明度100）', variable=self.semi).pack(side='left', padx=(18, 0))
        limits = ttk.Frame(frame)
        limits.pack(fill='x', pady=(5, 0))
        ttk.Label(limits, text='取得件数／スレッド（0＝全件要求）:').pack(side='left')
        ttk.Entry(limits, textvariable=self.max_comments, width=10).pack(side='left', padx=5)
        self.search_rows = []
        if DND_FILES:
            for key, var in self.paths.items():
                widget = settings.grid_slaves(row=list(self.paths).index(key), column=1)[0]
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self.on_drop)
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self.on_drop)
        qrow = ttk.Frame(frame)
        qrow.pack(fill='x', pady=(12, 5))
        self.query = tk.StringVar()
        entry = ttk.Entry(qrow, textvariable=self.query)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda _: self.search(False))
        ttk.Button(qrow, text='検索', command=lambda: self.search(False)).pack(side='left', padx=4)
        ttk.Button(qrow, text='次の50件', command=lambda: self.search(True)).pack(side='left')
        ttk.Label(frame, text='選択→保存で動画との対応を確認できます。ダブルクリックは1行を対象にします。' ).pack(anchor='w')
        self.tree = ttk.Treeview(frame, columns=('id', 'title', 'views', 'comments'), show='headings', selectmode='extended')
        for col, name, width in [('id','動画ID',115),('title','タイトル',510),('views','再生',90),('comments','コメント',90)]:
            self.tree.heading(col, text=name)
            self.tree.column(col, width=width, anchor='w' if col == 'title' else 'e')
        self.tree.pack(fill='both', expand=True, pady=6)
        self.tree.bind('<Double-1>', self.save_selected)
        ttk.Button(frame, text='選択した動画を一括保存', command=self.save_selected).pack(anchor='e')
        direct = ttk.Frame(frame)
        direct.pack(fill='x')
        self.direct = tk.StringVar()
        ttk.Entry(direct, textvariable=self.direct).pack(side='left', fill='x', expand=True)
        ttk.Button(direct, text='URL・IDから保存', command=self.save_direct).pack(side='left', padx=(4,0))
        self.cancel_button = ttk.Button(frame, text='取得・処理をキャンセル', command=self.cancel, state='disabled')
        self.cancel_button.pack(anchor='e', pady=4)
        self.progress_text = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self.progress_text, wraplength=1000).pack(anchor='w')
        self.status = tk.StringVar(value='準備完了')
        ttk.Label(frame, textvariable=self.status).pack(anchor='w', pady=(6,0))
        self.logbox = tk.Text(frame, height=6, state='disabled', wrap='word')
        self.logbox.pack(fill='x')
        self.protocol('WM_DELETE_WINDOW', self.close)

    def on_drop(self, event):
        for name in self.tk.splitlist(event.data):
            path = Path(name)
            if path.is_dir():
                self.paths['destination'].set(str(path))
            elif path.name.lower() == 'nndownload.py':
                self.paths['nndownload'].set(str(path))
            elif path.name.lower() == 'nicoconvass.exe':
                self.paths['nicoconvass'].set(str(path))
        return 'break'

    def apply_filter(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        displayed_ids = set()
        ordered = sorted(self.search_rows, key=lambda row: int(re.sub(r'\D', '', row.get('contentId', '')) or 0)) if self.only_so.get() else self.search_rows
        for row in ordered:
            vid = row.get('contentId', '')
            if not vid or vid in displayed_ids or (self.only_so.get() and not vid.lower().startswith('so')):
                continue
            if not self.include_danime.get() and str(row.get('channelId', '')).removeprefix('ch') == DANIME_CHANNEL_ID:
                continue
            title = row.get('title', '')
            item = self.tree.insert('', 'end', values=(vid, title, row.get('viewCounter', 0), row.get('commentCounter', 0)))
            self.rows[item] = (vid, title)
            displayed_ids.add(vid)
        self.status.set(f'{len(self.rows)} 件表示（取得済み {len(self.search_rows)} 件）')

    def refresh_search(self):
        if self.query.get().strip():
            self.search(False)
        else:
            self.apply_filter()

    def pick(self, var, kind):
        result = filedialog.askdirectory() if kind == 'directory' else filedialog.askopenfilename(filetypes=[('実行ファイル', '*.py *.exe'), ('すべて', '*.*')])
        if result: var.set(result)

    def log(self, message):
        def update():
            self.logbox.configure(state='normal')
            self.logbox.insert('end', message + '\n')
            if int(self.logbox.index('end-1c').split('.')[0]) > 2500:
                self.logbox.delete('1.0', '500.0')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')
        self.after(0, update)

    def cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state='disabled')
        self.status.set('キャンセル処理中…')

    def work(self, action, cancellable=False):
        if self.busy: return
        self.busy = True
        self.cancel_event.clear()
        self.cancel_button.configure(state='normal' if cancellable else 'disabled')
        def finish():
            self.busy = False
            self.cancel_button.configure(state='disabled')
        def wrapper():
            try: action()
            except Cancelled:
                self.after(0, lambda: self.status.set('キャンセルしました'))
            except Exception as ex:
                self.after(0, lambda e=str(ex): messagebox.showerror('処理に失敗しました', e))
                self.after(0, lambda e=str(ex): self.status.set('失敗: ' + e))
            finally: self.after(0, finish)
        threading.Thread(target=wrapper, daemon=True).start()

    def search(self, more):
        if self.busy: return
        word = self.query.get().strip()
        if not word: return
        if not more: self.offset = 0
        offset = self.offset
        include_danime = self.include_danime.get()
        self.status.set('検索中…')
        def action():
            if include_danime:
                rows, scanned = search_with_danime(word, offset)
                rows.sort(key=lambda row: int(row.get('viewCounter') or 0), reverse=True)
            else:
                rows = search_video(word, offset)
                scanned = len(rows)
            def show():
                if not more:
                    self.search_rows.clear()
                self.search_rows.extend(rows)
                self.apply_filter()
                self.offset = offset + scanned
                self.status.set(f'{len(self.rows)} 件表示（検索 {scanned} 件を確認）')
            self.after(0, show)
        self.work(action)

    def save_selected(self, _event=None):
        if _event is not None and self.tree.identify_region(_event.x, _event.y) != 'cell':
            return
        if _event is not None:
            row = self.tree.identify_row(_event.y)
            if row: self.tree.selection_set(row)
        selection = self.tree.selection()
        if selection:
            self.save_many([self.rows[item] for item in selection])

    def save_direct(self):
        try: vid = extract_id(self.direct.get())
        except ValueError as ex:
            messagebox.showerror('動画ID', str(ex)); return
        self.save_many([(vid, vid)])

    def save_many(self, items):
        if self.busy:
            return
        settings = {key: var.get() for key, var in self.paths.items()}
        try:
            count = int(self.max_comments.get())
            if count < 0 or count > 1000000:
                raise ValueError
        except ValueError:
            messagebox.showerror('コメント数', '0～1000000 の整数を入力してください。')
            return
        settings['max_comments'] = count
        settings.update(resolution=self.resolution.get(), semi=self.semi.get())
        if self.only_so.get():
            items = [(vid, title) for vid, title in items if vid.lower().startswith('so')]
        if not items:
            messagebox.showinfo('対象なし', '保存する so 動画を選択してください。')
            return
        folder = settings['destination'].strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror('フォルダ', 'ASS保存先・動画名の参照フォルダを指定してください。')
            return
        self.status.set('動画フォルダを照合中…')
        def scan():
            index = video_index(folder, self.cancel_event)
            results = [match_video(title, index) for _, title in items]
            check_cancel(self.cancel_event)
            self.after(0, lambda: self.preview(items, results, settings))
        self.work(scan, cancellable=True)

    def preview(self, items, results, settings):
        matches = [path for path, _, _ in results]
        kinds = [kind for _, kind, _ in results]
        notes = [note for _, _, note in results]
        window = tk.Toplevel(self)
        window.title('動画との対応を確認')
        window.geometry('1100x500')
        window.transient(self)
        window.grab_set()
        ttk.Label(window, text='未一致・候補が複数ある行はスキップします。行をダブルクリックすると動画を指定できます。\n'
                               '「推定・要確認」の行は対応を確かめてから「推定を承認」を押すと保存対象になります。').pack(anchor='w', padx=12, pady=8)
        table = ttk.Treeview(window, columns=('title', 'target', 'status'), show='headings', selectmode='extended')
        for key, title, width in [('title', 'ニコニコのタイトル', 330), ('target', '保存するASS（参照動画と同じ場所）', 470), ('status', '状態', 230)]:
            table.heading(key, text=title)
            table.column(key, width=width)
        table.pack(fill='both', expand=True, padx=12)
        scrollbar = ttk.Scrollbar(window, orient='horizontal', command=table.xview)
        scrollbar.pack(fill='x', padx=12)
        table.configure(xscrollcommand=scrollbar.set)
        def refresh():
            selected = table.selection()
            table.delete(*table.get_children())
            targets = [str(p.with_suffix('.ass')).casefold() if p else None for p in matches]
            for i, ((vid, title), path) in enumerate(zip(items, matches)):
                status = '未一致・スキップ' + (f'（{notes[i]}）' if notes[i] else '')
                if path:
                    status = '保存対象' + ('（フォルダ名で一致）' if kinds[i] == 'folder' else '')
                    if kinds[i] not in CONFIRMED_KINDS:
                        status = f'推定・要確認（{notes[i]}）'
                    elif path.with_suffix('.ass').exists():
                        status = '既存・スキップ'
                    elif targets.count(targets[i]) > 1:
                        status = '重複・要指定'
                table.insert('', 'end', iid=str(i), values=(title, str(path.with_suffix('.ass')) if path else '', status))
            for iid in selected:
                if table.exists(iid): table.selection_add(iid)
        def choose(event=None):
            if event is not None and table.identify_region(event.x, event.y) != 'cell': return
            selected = table.selection()
            if len(selected) != 1: return
            result = filedialog.askopenfilename(parent=window, title='対応する動画を選択', initialdir=settings['destination'], filetypes=[('動画', ' '.join('*'+e for e in sorted(VIDEO_EXTENSIONS)))])
            if result:
                if not Path(result).resolve().is_relative_to(Path(settings['destination']).expanduser().resolve()):
                    messagebox.showerror('参照フォルダ', '指定した保存・参照フォルダ内の動画を選んでください。', parent=window)
                    return
                matches[int(selected[0])] = Path(result)
                kinds[int(selected[0])] = 'manual'
                refresh()
        def clear():
            for iid in table.selection():
                matches[int(iid)] = None
                kinds[int(iid)] = None
                notes[int(iid)] = '手動でスキップ'
            refresh()
        def approve():
            for iid in table.selection():
                if matches[int(iid)]: kinds[int(iid)] = 'manual'
            refresh()
        def start():
            jobs = [(vid, title, matches[i]) for i, (vid, title) in enumerate(items) if table.set(str(i), 'status').startswith('保存対象')]
            if not jobs:
                messagebox.showinfo('対象なし', '保存対象がありません。対応する動画を指定してください。', parent=window)
                return
            window.destroy()
            self.execute_items(jobs, settings)
        table.bind('<Double-1>', choose)
        buttons = ttk.Frame(window, padding=12)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='動画を指定', command=choose).pack(side='left')
        ttk.Button(buttons, text='推定を承認', command=approve).pack(side='left', padx=(6, 0))
        ttk.Button(buttons, text='選択行をスキップ', command=clear).pack(side='left', padx=6)
        ttk.Button(buttons, text='保存対象を一括実行', command=start).pack(side='right')
        refresh()
        self.status.set('対応する動画を確認してください')

    def execute_items(self, items, settings):
        if self.busy: return
        (APP / 'work').mkdir(exist_ok=True)
        self.status.set(f'{len(items)} 件の保存を開始')
        def action():
            successes, errors = [], []
            cancelled = False
            for index, (vid, title, video) in enumerate(items, 1):
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                self.after(0, lambda n=index, v=vid: self.status.set(f'{n}/{len(items)}: {v} を処理中'))
                try:
                    prefix = f'{index}/{len(items)}: {Path(video).name} [{vid}]'
                    def progress(text, label=prefix):
                        self.after(0, lambda value=label + '\n' + text: self.progress_text.set(value))
                    target = convert(vid, title, settings, self.log, video, self.cancel_event, progress)
                    successes.append(target)
                    self.log('保存完了: ' + str(target))
                except Cancelled:
                    cancelled = True
                    self.log('キャンセルしました。残りの動画は処理しません。')
                    break
                except Exception as ex:
                    errors.append(f'{vid}: {ex}')
                    self.log('失敗: ' + errors[-1])
            summary = f'保存完了 {len(successes)} 件 / 失敗 {len(errors)} 件' + (' / キャンセル' if cancelled else '')
            self.after(0, lambda: self.status.set(summary))
            self.after(0, lambda: messagebox.showinfo('一括保存結果', summary + ('\n' + '\n'.join(errors[:8]) if errors else '')))
        self.work(action, cancellable=True)

    def close(self):
        if self.busy:
            messagebox.showinfo('処理中', '処理が完了してから閉じてください。')
            return
        CONFIG.write_text(json.dumps({**{key: var.get() for key,var in self.paths.items()}, 'only_so': self.only_so.get(), 'include_danime': self.include_danime.get(), 'download_limit': self.max_comments.get(), 'resolution': self.resolution.get(), 'semi': self.semi.get()}, ensure_ascii=False, indent=2), encoding='utf-8')
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
