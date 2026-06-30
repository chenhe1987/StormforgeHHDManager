import subprocess
ps_cmd = """
$devices = Get-PnpDevice -Class DiskDrive -ErrorAction SilentlyContinue | Where-Object { $_.Problem -ne 0 -and $_.Problem -ne $null -and $_.Problem -ne 'CM_PROB_PHANTOM' }
Write-Output $devices.Count
"""
print(subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True).stdout)
