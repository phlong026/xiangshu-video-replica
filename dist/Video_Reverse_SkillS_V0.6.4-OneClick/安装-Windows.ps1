#requires -Version 5.1

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot

function Add-UserPathEntry {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    $alreadyPresent = $entries | Where-Object {
        $_.TrimEnd("\") -ieq $Directory.TrimEnd("\")
    }
    if (-not $alreadyPresent) {
        [Environment]::SetEnvironmentVariable(
            "Path",
            (@($entries + $Directory) -join ";"),
            "User"
        )
    }
}

function Refresh-ProcessPath {
    $wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
    if (Test-Path -LiteralPath $wingetLinks) {
        Add-UserPathEntry $wingetLinks
    }

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
}

function Invoke-LoggedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$LogName,
        [int]$TimeoutSeconds = 900
    )

    $logDirectory = Join-Path $PSScriptRoot "install-logs"
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stdoutPath = Join-Path $logDirectory ($LogName + ".stdout.log")
    $stderrPath = Join-Path $logDirectory ($LogName + ".stderr.log")

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $PSScriptRoot `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill()
        throw "$LogName 超过 $TimeoutSeconds 秒未完成，已终止。"
    }

    if (Test-Path -LiteralPath $stdoutPath) {
        Get-Content -LiteralPath $stdoutPath | ForEach-Object { Write-Host $_ }
    }
    if ($process.ExitCode -ne 0) {
        $detail = ""
        if (Test-Path -LiteralPath $stderrPath) {
            $detail = (Get-Content -LiteralPath $stderrPath -Raw).Trim()
        }
        throw "$LogName 失败（退出码 $($process.ExitCode)）。$detail"
    }
}

function Get-PythonRunner {
    $candidates = @()
    $knownLauncher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
    if (Test-Path -LiteralPath $knownLauncher) {
        $candidates += [PSCustomObject]@{ Executable = $knownLauncher; Arguments = @("-3.11") }
        $candidates += [PSCustomObject]@{ Executable = $knownLauncher; Arguments = @("-3") }
    }

    foreach ($knownPython in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe")
    )) {
        if (Test-Path -LiteralPath $knownPython) {
            $candidates += [PSCustomObject]@{ Executable = $knownPython; Arguments = @() }
        }
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += [PSCustomObject]@{ Executable = $launcher.Source; Arguments = @("-3.11") }
        $candidates += [PSCustomObject]@{ Executable = $launcher.Source; Arguments = @("-3") }
    }

    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $candidates += [PSCustomObject]@{ Executable = $command.Source; Arguments = @() }
        }
    }

    foreach ($candidate in $candidates) {
        & $candidate.Executable @($candidate.Arguments) -c `
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    return $null
}

function Test-FFmpeg {
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    $ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
    if (-not $ffmpeg -or -not $ffprobe) {
        return $false
    }
    & $ffmpeg.Source -version *> $null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    & $ffprobe.Source -version *> $null
    return $LASTEXITCODE -eq 0
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$PackageId,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "未找到 winget。请先从 Microsoft Store 安装 Microsoft App Installer，再重新运行本安装包。"
    }

    Write-Host "正在通过 WinGet 下载并安装 $DisplayName ..."
    $arguments = @(
        "install",
        "--id", $PackageId,
        "--exact",
        "--source", "winget",
        "--scope", "user",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--silent",
        "--disable-interactivity"
    )
    Invoke-LoggedProcess $winget.Source $arguments ("winget-" + $PackageId) 900
    Refresh-ProcessPath
}

try {
    Refresh-ProcessPath

    $python = Get-PythonRunner
    if (-not $python) {
        Install-WingetPackage "Python.Python.3.11" "Python 3.11"
        $python = Get-PythonRunner
        if (-not $python) {
            throw "Python 3.11 已执行安装，但当前用户仍无法调用 Python 3.9+。"
        }
    } else {
        Write-Host "已检测到 Python 3.9+。"
    }
    Add-UserPathEntry (Split-Path -Parent $python.Executable)
    Refresh-ProcessPath

    if (-not (Test-FFmpeg)) {
        Install-WingetPackage "Gyan.FFmpeg" "FFmpeg"
        if (-not (Test-FFmpeg)) {
            throw "FFmpeg 已执行安装，但 ffmpeg/ffprobe 仍不可用。"
        }
    } else {
        Write-Host "已检测到 FFmpeg 与 ffprobe。"
    }

    $installer = Join-Path $PSScriptRoot "install_skill.py"
    $pythonArguments = @($python.Arguments) + @('"' + $installer + '"')
    Invoke-LoggedProcess $python.Executable $pythonArguments "skill-install" 180
    Write-Host "Skill 及运行环境已安装完成，请重启 Codex。"
    exit 0
} catch {
    [Console]::Error.WriteLine("安装失败：" + $_.Exception.Message)
    exit 1
}
