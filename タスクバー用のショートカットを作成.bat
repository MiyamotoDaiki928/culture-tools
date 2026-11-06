@echo off
chcp 65001 >nul
call "%~dp0app\_setup.bat" || exit /b 1
rem 最初に 1 回だけ使う。app の中に実行環境を用意し、スタートメニューに culture-tools のショートカットを作る
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\make_shortcut.ps1"
if errorlevel 1 (
  echo ショートカットを作れませんでした。
  pause
  exit /b 1
)
echo.
echo 開いたフォルダで culture-tools を右クリックし、「タスクバーにピン留めする」を選んでください。
echo （Windows 11 で見当たらない場合は、右クリックのあと「その他のオプションを確認」の中にあります）
echo.
pause
