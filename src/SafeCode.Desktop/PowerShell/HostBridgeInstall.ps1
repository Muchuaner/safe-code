$ErrorActionPreference='Stop'
$root=[Environment]::GetFolderPath('CommonApplicationData')+'\SafeCodeHostBridge'
$r=Get-Content -LiteralPath ($root+'\request.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName='SafeCode-HostBridge'
$existing=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if($existing -and $existing.State -eq 'Running'){throw '本机控制服务仍在使用，请先停用并等待结束。'}
$bin=$root+'\bin\OpenSSH-Win64'
foreach($name in @('sshd.exe','sshd-auth.exe','sshd-session.exe','ssh-keygen.exe')) {
 $signature=Get-AuthenticodeSignature -LiteralPath ($bin+'\'+$name)
 if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)'){throw 'OpenSSH Microsoft signature validation failed.'}
}
function PrivateFile($path) {
 $acl=New-Object Security.AccessControl.FileSecurity
 $acl.SetAccessRuleProtection($true,$false)
 $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
 foreach($sid in @('S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),'FullControl','Allow')))}
 Set-Acl -LiteralPath $path -AclObject $acl
}
foreach($name in @('sshd.log','sshd-out.log')){$path=$root+'\'+$name;if(-not(Test-Path -LiteralPath $path)){[IO.File]::WriteAllText($path,'')};PrivateFile $path}
$hostKey=$root+'\host_ed25519'
if(-not(Test-Path -LiteralPath $hostKey)) {
 & "$bin\ssh-keygen.exe" -q -t ed25519 -N '""' -C safe-code-host -f $hostKey
 if($LASTEXITCODE -ne 0){throw 'SSH host key generation failed.'}
}
PrivateFile $hostKey
[IO.File]::WriteAllText($root+'\authorized_keys',$r.PublicKey+"`n",[Text.Encoding]::ASCII)
PrivateFile ($root+'\authorized_keys')
$p=$root.Replace('\','/')
$user=$r.Username.ToLowerInvariant()
$config=@"
Port $($r.Port)
AddressFamily inet
ListenAddress 127.0.0.1
HostKey "$p/host_ed25519"
PidFile "$p/sshd.pid"
AuthorizedKeysFile "$p/authorized_keys"
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
AllowUsers $user
AllowAgentForwarding no
AllowTcpForwarding no
GatewayPorts no
PermitTunnel no
X11Forwarding no
PermitUserEnvironment no
StrictModes yes
LogLevel VERBOSE
Subsystem sftp "$p/bin/OpenSSH-Win64/sftp-server.exe"
"@
[IO.File]::WriteAllText($root+'\sshd_config',$config,[Text.Encoding]::ASCII)
& "$bin\sshd.exe" -t -f ($root+'\sshd_config')
if($LASTEXITCODE -ne 0){throw 'SSH server configuration validation failed.'}
$lease=$root+'\lease'
[IO.File]::WriteAllText($lease,$r.Nonce,[Text.Encoding]::ASCII)
$acl=Get-Acl -LiteralPath $lease
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($r.Sid)),'Modify','Allow')))
Set-Acl -LiteralPath $lease -AclObject $acl
[IO.File]::WriteAllText($root+'\active.json',(@{Sid=$r.Sid;Nonce=$r.Nonce;Lease=$lease;Port=$r.Port}|ConvertTo-Json),[Text.Encoding]::UTF8)
Remove-Item -LiteralPath ($root+'\stopped.txt') -Force -ErrorAction SilentlyContinue
$broker=Get-Content -LiteralPath ($root+'\broker.ps1') -Raw -Encoding UTF8
$encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($broker))
$action=New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -EncodedCommand '+$encoded)
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
# No startup trigger and no firewall rule. A lease expires 30s after the app stops.
$ready=$false
for($i=0;$i -lt 15;$i++) {
 Start-Sleep -Milliseconds 500
 $listener=Get-NetTCPConnection -State Listen -LocalPort $r.Port -ErrorAction SilentlyContinue
 if($listener -and @($listener | Where-Object LocalAddress -ne '127.0.0.1').Count -gt 0){throw 'SSH server is not limited to loopback.'}
 if($listener){$ready=$true;break}
}
if(-not $ready){throw 'SSH server did not start. Please inspect the dedicated OpenSSH log.'}
[IO.File]::WriteAllText($root+'\result.json',(@{Nonce=$r.Nonce;Port=$r.Port;Username=$r.Username;HostKey=([IO.File]::ReadAllText($hostKey+'.pub').Trim());Ready=$true}|ConvertTo-Json),[Text.Encoding]::UTF8)
