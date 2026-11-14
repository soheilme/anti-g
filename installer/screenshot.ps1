param([string]$ProcessName = "AI-Tool", [string]$OutFile = "shot.png")
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WinShotTool {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[WinShotTool]::SetProcessDPIAware() | Out-Null
$proc = Get-Process $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -ne "" } | Select-Object -First 1
if (-not $proc) { Write-Output "window not found"; exit 1 }
$h = [IntPtr]$proc.MainWindowHandle
[WinShotTool]::ShowWindow($h, 9) | Out-Null
[WinShotTool]::SetWindowPos($h, [IntPtr]::new(-1), 0, 0, 0, 0, 0x0003) | Out-Null
Start-Sleep -Milliseconds 1500
$rect = New-Object WinShotTool+RECT
[WinShotTool]::GetWindowRect($h, [ref]$rect) | Out-Null
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
$bmp = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bmp)
$graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size $width, $height))
$path = Join-Path $PSScriptRoot $OutFile
$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
[WinShotTool]::SetWindowPos($h, [IntPtr]::new(-2), 0, 0, 0, 0, 0x0003) | Out-Null
Write-Output ("saved {0} {1}x{2}" -f $path, $width, $height)
