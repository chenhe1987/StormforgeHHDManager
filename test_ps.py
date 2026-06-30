import subprocess
ps_cmd = """
$devices = Get-PnpDevice -Class DiskDrive -ErrorAction SilentlyContinue | Where-Object { $_.Problem -ne 0 -and $_.Problem -ne $null }
foreach ($dev in $devices) {
    Write-Output "$($dev.InstanceId)|$($dev.Problem)|$($dev.Present)"
}
"""
print(subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True).stdout)
