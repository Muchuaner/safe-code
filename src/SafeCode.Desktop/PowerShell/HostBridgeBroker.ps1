$ErrorActionPreference='Stop'
$root=[Environment]::GetFolderPath('CommonApplicationData')+'\SafeCodeHostBridge'
$r=Get-Content -LiteralPath ($root+'\active.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# A kernel job closes all SSH children even if the broker itself is killed.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SafeCodeLeaseJob {
 [StructLayout(LayoutKind.Sequential)] struct Basic {public long ProcessTime,JobTime;public uint Flags;public UIntPtr Min,Max;public uint Processes;public UIntPtr Affinity;public uint Priority,Scheduling;}
 [StructLayout(LayoutKind.Sequential)] struct Counters {public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes;}
 [StructLayout(LayoutKind.Sequential)] struct Limits {public Basic Basic;public Counters IO;public UIntPtr ProcessMemory,JobMemory,PeakProcessMemory,PeakJobMemory;}
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int info,IntPtr value,uint size);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
 public static IntPtr Attach(IntPtr process) {
  IntPtr job=CreateJobObject(IntPtr.Zero,null);if(job==IntPtr.Zero)throw new InvalidOperationException("Cannot create SSH lifetime job");
  var limits=new Limits();limits.Basic.Flags=0x2000;int size=Marshal.SizeOf(typeof(Limits));IntPtr memory=Marshal.AllocHGlobal(size);
  try {Marshal.StructureToPtr(limits,memory,false);if(!SetInformationJobObject(job,9,memory,(uint)size)||!AssignProcessToJobObject(job,process)){CloseHandle(job);throw new InvalidOperationException("Cannot isolate SSH lifetime");}return job;}
  finally {Marshal.FreeHGlobal(memory);}
 }
}
'@
$server=$null
$job=[IntPtr]::Zero
try {
 $sshd=$root+'\bin\OpenSSH-Win64\sshd.exe'
 $server=Start-Process -FilePath $sshd -ArgumentList @('-D','-e','-f',('"'+$root+'\sshd_config"')) -WindowStyle Hidden -RedirectStandardError ($root+'\sshd.log') -RedirectStandardOutput ($root+'\sshd-out.log') -PassThru
 $job=[SafeCodeLeaseJob]::Attach($server.Handle)
 while(-not $server.HasExited) {
  if(-not(Test-Path -LiteralPath $r.Lease)){break}
  if(([DateTime]::UtcNow-(Get-Item -LiteralPath $r.Lease).LastWriteTimeUtc).TotalSeconds -gt 30){break}
  Start-Sleep -Seconds 1
  $server.Refresh()
 }
} finally {
 if($job -ne [IntPtr]::Zero){[SafeCodeLeaseJob]::CloseHandle($job) | Out-Null}
 if($null -ne $server -and -not $server.HasExited){& "$env:SystemRoot\System32\taskkill.exe" /PID $server.Id /T /F | Out-Null}
 Remove-Item -LiteralPath ($root+'\authorized_keys') -Force -ErrorAction SilentlyContinue
 [IO.File]::WriteAllText($root+'\stopped.txt',$r.Nonce,[Text.Encoding]::UTF8)
}
