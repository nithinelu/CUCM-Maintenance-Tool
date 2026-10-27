# Adds "CUCM Maintenance Tool" to the Start Menu and Desktop (current user only).
# Run:  powershell -ExecutionPolicy Bypass -File install_shortcuts.ps1
# Remove: delete the two "CUCM Maintenance Tool.lnk" files.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $env:LOCALAPPDATA "Python\bin\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\pythonw.exe" }
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found - install Python and run setup.bat first." }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "CUCM Maintenance Tool.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "CUCM Maintenance Tool.lnk")
)
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $here "maint_app.py") + '"'
    $s.WorkingDirectory = $here
    $s.IconLocation = (Join-Path $here "app.ico") + ",0"
    $s.Description = "CUCM backup, upgrade, COP install and export with built-in SFTP server"
    $s.Save()
    "Created $lnk"
}
