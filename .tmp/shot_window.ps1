Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinCap {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern IntPtr FindWindowW(string cls, string title);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[void][WinCap]::SetProcessDPIAware()
$h = [IntPtr]::Zero
$procs = Get-Process chrome -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
foreach ($p in $procs) {
  $t = $p.MainWindowTitle
  if ($t -and $t -like "*Google Chrome*" -and $t -notlike "*H3*") { $h = $p.MainWindowHandle; break }
}
if ($h -eq [IntPtr]::Zero) { Write-Output "WINDOW_NOT_FOUND"; exit 1 }
[void][WinCap]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 400
$r = New-Object WinCap+RECT
[void][WinCap]::GetWindowRect($h, [ref]$r)
$w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
Write-Output "rect=$($r.Left),$($r.Top) ${w}x${ht}"
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap $w, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
$out = "C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png"
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "saved $out"
