Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinEnum {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder sb, int max);
}
"@
$cb = [WinEnum+EnumWindowsProc]{ param($h, $l)
  if ([WinEnum]::IsWindowVisible($h)) {
    $t = New-Object System.Text.StringBuilder 256
    [void][WinEnum]::GetWindowTextW($h, $t, 256)
    $c = New-Object System.Text.StringBuilder 256
    [void][WinEnum]::GetClassNameW($h, $c, 256)
    Write-Output ("0x{0:X8} [{1}] {2}" -f $h.ToInt64(), $c.ToString(), $t.ToString())
  }
  return $true
}
[void][WinEnum]::EnumWindows($cb, [IntPtr]::Zero)
