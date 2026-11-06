# Create the culture-tools shortcut in the Start menu and remove the old desktop shortcuts.
$dir = $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'culture-tools.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $dir '.venv\Scripts\pythonw.exe'
$s.Arguments = '"' + (Join-Path $dir 'culture_app.py') + '"'
$s.WorkingDirectory = $dir
$s.IconLocation = (Join-Path $dir 'culture-tools.ico') + ',0'
$s.Description = 'culture-tools'
$s.Save()
$desktop = [Environment]::GetFolderPath('Desktop')
foreach ($n in 'culture-stitcher', 'culture-viewer', 'culture-folders') {
    Remove-Item -LiteralPath (Join-Path $desktop ($n + '.lnk')) -ErrorAction SilentlyContinue
}
Start-Process explorer.exe -ArgumentList ('/select,"' + $lnk + '"')
