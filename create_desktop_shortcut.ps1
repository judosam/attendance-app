$wsh = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Attendance Report.lnk'
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'D:\My workspace\Projects\Report Generate\biometric_timechamp_report\run.bat'
$shortcut.WorkingDirectory = 'D:\My workspace\Projects\Report Generate\biometric_timechamp_report'
$shortcut.Description = 'Start Attendance Report Generator'
$shortcut.IconLocation = 'C:\Users\Samuel P\AppData\Local\Programs\Python\Python312\python.exe,0'
$shortcut.Save()
Write-Host "Desktop shortcut created at: $shortcutPath"
