# ニコニコ コメント保存ツール（nico_comment_tool）

ニコニコ動画のコメントを取得して ASS 字幕に変換し、手元の動画ファイルと同じ名前で保存する Windows 向け GUI ツールです。
動画のタイトルとファイル名から「作品名＋話数」を読み取って自動で照合します。

## 必要なもの
- Windows 10 / 11
- [uv](https://docs.astral.sh/uv/)（Python 3.11 と tkinterdnd2 を自動で用意します）
- [nndownload](https://github.com/AlexAplin/nndownload)（コメント取得）
- NicoConvAss（コメント XML → ASS 変換。Windows 用フリーソフト）
- Firefox（ニコニコにログインしておく）

## インストール
1. このリポジトリを ZIP でダウンロードして展開するか、`git clone` します。
2. uv をインストールします（PowerShell）：
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. nndownload を GitHub から ZIP でダウンロードして任意のフォルダに展開します。
   依存ライブラリ（nndownload の `requirements.txt`）は、コメント取得時に uv が自動で用意します。
4. NicoConvAss を入手して任意のフォルダに展開します。
5. `起動.vbs` をダブルクリックして起動します（コンソールは表示しません。失敗時は `startup.log` を確認）。
6. 初回は画面上で次の3つを指定します（`settings.json` に保存されます）。
   - nndownload の `nndownload.py`
   - `NicoConvAss.exe`
   - ASS の保存先＝動画のあるフォルダ

## 使い方
詳しい操作・照合ルールは [README.txt](README.txt) を参照してください。
作業中のファイルは `work/` に作られます（不要になったら削除できます）。

## ライセンス
MIT License（[LICENSE](LICENSE)）。nndownload・NicoConvAss はそれぞれの作者のライセンスに従います（このリポジトリには含みません）。
ニコニコ動画の利用規約を守って使用してください。
