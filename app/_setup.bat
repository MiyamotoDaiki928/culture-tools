@echo off
rem 初回のみ: このフォルダに専用の Python 実行環境 (.venv) を作り、必要なライブラリを入れる
if exist "%~dp0.venv\Scripts\python.exe" exit /b 0
echo 初回セットアップ中です（数分かかります）...
where py >nul 2>nul && (py -3 -m venv "%~dp0.venv") || (python -m venv "%~dp0.venv")
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Python が見つかりません。https://www.python.org/ からインストールしてください。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install --quiet -r "%~dp0requirements.txt"
exit /b %errorlevel%
