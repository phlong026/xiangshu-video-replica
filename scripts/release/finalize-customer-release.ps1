# 在可信仓库运行；MacArtifactsDir 仅是隔离提取的数据目录，绝不从中执行代码。
# MacArtifactsDir 接受 desktop-macos-arm64 与/或 desktop-macos-x86_64 的任意非空
# 子集（x64 macOS 编译层问题未解，2.6.8 允许只发 arm64）；提供哪个 manifest
# SHA256 pin 就校验哪个平台：-MacArm64ManifestSha256 必填，-MacX64ManifestSha256
# 可选。pin 必须来自已核验 CI commit/archive digest 的可信下载记录。
# 输出目录平铺实际提供平台的资产、stable.json、release-manifest.json、SHA256SUMS.txt。
# 私钥只由本机通用 Tauri signer 通过已有环境读取，CI 不参与签名。
param(
  [Parameter(Mandatory)][string]$WindowsReleaseDir,
  [Parameter(Mandatory)][string]$MacArtifactsDir,
  [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedCommit,
  [Parameter(Mandatory)][string]$Version,
  [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{64}$')][string]$MacArm64ManifestSha256,
  [ValidatePattern('^[a-f0-9]{64}$')][string]$MacX64ManifestSha256,
  [Parameter(Mandatory)][string]$OutputDir,
  [string]$Origin = 'https://video.zszhj.cn'
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw '正式汇总须在 Windows 本机复核 Authenticode gate' }
if (-not $env:TAURI_SIGNING_PRIVATE_KEY) { throw 'TAURI_SIGNING_PRIVATE_KEY is required' }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$core = Join-Path $PSScriptRoot 'finalize-customer-release.mjs'
$tauriCli = Join-Path $repoRoot 'node_modules\@tauri-apps\cli\tauri.js'
if (-not (Test-Path -LiteralPath $tauriCli -PathType Leaf)) { throw '可信仓库缺少本地 Tauri CLI；请先安装锁定依赖，签名时禁止联网拉取工具' }
$windowsInput = (Resolve-Path -LiteralPath $WindowsReleaseDir).Path
$macInput = (Resolve-Path -LiteralPath $MacArtifactsDir).Path
$output = [System.IO.Path]::GetFullPath($OutputDir)
if (Test-Path -LiteralPath $output) {
  $item = Get-Item -LiteralPath $output -Force
  if (-not $item.PSIsContainer -or $item.LinkType -or @(Get-ChildItem -LiteralPath $output -Force).Count) {
    throw '输出目录必须是新的空普通目录'
  }
}
$parent = Split-Path -Parent $output
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$stage = Join-Path $parent ('.customer-finalize-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
Push-Location $repoRoot
try {
  $prepareArgs = @(
    $core, 'prepare', '--windows-release-dir', $windowsInput, '--mac-artifacts-dir', $macInput,
    '--expected-commit', $ExpectedCommit, '--version', $Version, '--origin', $Origin,
    '--mac-arm64-manifest-sha256', $MacArm64ManifestSha256, '--stage-dir', $stage
  )
  # 缺 x64 pin 即只发 arm64 子集；pin 仅在实际提供对应平台时参与校验。
  if ($MacX64ManifestSha256) { $prepareArgs += @('--mac-x64-manifest-sha256', $MacX64ManifestSha256) }
  $planText = & node @prepareArgs
  if ($LASTEXITCODE -ne 0) { throw '发行输入、可信出处或 Windows updater 独立验签失败' }
  $plan = ($planText -join "`n") | ConvertFrom-Json

  # 对将要分发的同一份快照重走旧 Windows gate，不能拿另一文件的签名报告代替。
  $signature = Get-AuthenticodeSignature -LiteralPath $plan.windowsInstaller
  if ($signature.Status -ne 'Valid') { throw "Windows Authenticode signature is not valid: $($signature.Status)" }
  if (-not $plan.windowsThumbprint -or $signature.SignerCertificate.Thumbprint -ne $plan.windowsThumbprint) {
    throw 'Windows 签名证书与正式发行记录不匹配'
  }
  if ($env:VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER -and
    -not $signature.SignerCertificate.Subject.Contains($env:VIDEO_REPLICA_RELEASE_EXPECTED_SIGNER)) {
    throw 'Windows 签名主体不匹配'
  }
  foreach ($archive in $plan.macArchives) {
    # 仅调用可信本地 CLI；密钥不出现在参数或日志里，不调用 updater 专用旧子命令。
    & node $tauriCli signer sign $archive | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Mac updater 归档签名失败' }
  }
  & node $core finalize --stage-dir $stage
  if ($LASTEXITCODE -ne 0) { throw '各平台独立验签或最终清单生成失败' }
  if (Test-Path -LiteralPath $output) {
    # 无 -Recurse：若其他进程写入了内容即失败，不覆盖已有发行目录。
    Remove-Item -LiteralPath $output
  }
  [System.IO.Directory]::Move($stage, $output)
  Write-Host "正式发行汇总完成（windows-x86_64 + $($plan.macArchives.Count) 个 Mac 平台）: $output"
} finally {
  Pop-Location
  if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
