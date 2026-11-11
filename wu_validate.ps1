#requires -Version 5.1
<#
.SYNOPSIS
    Xuan / WU 一次性验证包（PowerShell 5.1）。

.DESCRIPTION
    默认调用会把自身以隐藏窗口后台启动，立即返回；真正的验证在 -Worker
    进程中执行，日志写到 logs\validate_20261106e\。

    验证内容：
      1. 备份 backend\rules.yaml、crawler\windows.yaml 和现有代码到
         _backup_20261106e\，然后解压覆盖 code_20261106e.zip；
      2. backend\.venv 不存在时，用 crawler\.venv 同版本 python 创建
         backend\.venv 并安装 backend 依赖；失败则回退 crawler\.venv
         跑 backend pytest 与打分，并在 SUMMARY 写明；
      3. 在 crawler / backend 的 .venv 补装 requirements，跑三套 pytest；
      4. 用 windows.yaml 的放慢节奏小量采集“星星人”（京东→1688→淘宝，
         每平台 1 页，详情最多 5 个），risk_blocked 平台跳过；
         1688 完整详情本轮停用（crawl.detail.platforms.1688=false），列表照采；
         命中关键词且缺店名的候选按配置上限补抓店名（详情页/店铺页，复用降频）；
      5. 跑 crawler\tools\trial_metrics.py（按 platform+product_id 去重，
         每平台列列表/详情/评论数/店铺名非空率/候选店名非空率/模糊值指标
         和 3 条样例）；
      6. 临时给两份 yaml 加“星星人”IP，跑 backend dry-run 打分，
         按“本轮采集”和“含历史数据”两个口径统计 text_score / need_vision /
         候选并输出 ultraman_score.md；
      7. 从 .bak 还原 yaml 并做 MD5 校验；
      8. 无论成功失败都写 SUMMARY.md 和 DONE.txt。

    不修改业务代码；不写 backend\data\leads.db；不发邮件；不强杀 Edge，
    不使用 taskkill / Stop-Process。需要优雅退出时，只对本脚本自己启动的
    端口实例调用 crawler\.venv 的 wu_tools\cdp_close.py 发 CDP Browser.close
    （不再使用 PowerShell Add-Type System.Net.WebSockets.Client）。

.PARAMETER Worker
    内部使用：真正执行验证，不再后台拉起自身。

.PARAMETER NoBackground
    内部/调试使用：前台执行验证。

.PARAMETER Root
    项目根目录；默认取脚本所在目录。

.PARAMETER Date
    验证日期，默认 2026-11-06（与包名/目录名一致；本包为 e 轮）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File wu_validate.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File wu_validate.ps1 -NoBackground
#>
[CmdletBinding()]
param(
    [switch]$Worker,
    [switch]$NoBackground,
    [string]$Root = '',
    [string]$Date = '2026-11-06'
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

if (-not $Root) {
    if ($PSScriptRoot) { $Root = $PSScriptRoot }
    elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
    else { $Root = (Get-Location).Path }
}
$script:Root = [System.IO.Path]::GetFullPath($Root)
if ($script:Root.Length -gt 3) { $script:Root = $script:Root.TrimEnd([char[]]@('\', '/')) }
$script:Date = $Date
# 包名是唯一版本源：目录/日志/备份/标题都从 zip 文件名派生，避免像 c 轮
# 那样标题写死、包名却换成 d。
$script:PackageZipName = 'code_20261106e.zip'
$script:PackageBaseName = [System.IO.Path]::GetFileNameWithoutExtension($script:PackageZipName)
$script:PackageSuffix = $script:PackageBaseName.Substring($script:PackageBaseName.Length - 1)
$script:PackageVersionRaw = $script:PackageBaseName -replace '^code_', ''
$script:ValidateVersion = $script:PackageVersionRaw
$script:TitleVersion = $script:Date + $script:PackageSuffix
$script:LogRel = 'logs\validate_' + $script:ValidateVersion
$script:OutRel = 'out\validate_' + $script:ValidateVersion
$script:BackupRel = '_backup_' + $script:ValidateVersion
$script:PackageZipPath = Join-Path $script:Root $script:PackageZipName
$script:LogDir = Join-Path $script:Root $script:LogRel
$script:SummaryPath = Join-Path $script:LogDir 'SUMMARY.md'
$script:DonePath = Join-Path $script:LogDir 'DONE.txt'
$script:LogPath = Join-Path $script:LogDir 'validate.log'
$script:CommandLog = Join-Path $script:LogDir 'commands.log'
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
$script:Errors = @()
$script:Steps = @()
$script:StartedAt = Get-Date
$script:FatalError = $null
$script:PytestResults = @{}
$script:CrawlResults = @()
$script:CloseResults = @()
$script:TrialMetrics = $null
$script:Ultraman = $null
$script:RestoreResults = @()
$script:BackendPython = $null
$script:BackendPythonSource = 'unknown'
$script:BackendFallback = $false
$script:BackendNote = ''
$script:BackupDir = Join-Path $script:Root $script:BackupRel
$script:RulesPath = Join-Path $script:Root 'backend\config\rules.yaml'
$script:WindowsPath = Join-Path $script:Root 'crawler\config\windows.yaml'

$script:ScriptPath = $PSCommandPath
if (-not $script:ScriptPath) { $script:ScriptPath = $MyInvocation.MyCommand.Path }
if ($script:ScriptPath) { $script:ScriptPath = [System.IO.Path]::GetFullPath($script:ScriptPath) }

# ---------------------------------------------------------------------------
# 基础日志 / 状态
# ---------------------------------------------------------------------------
function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$Level = 'INFO'
    )
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        [System.IO.File]::AppendAllText($script:LogPath, $line + "`r`n", $script:Utf8NoBom)
    } catch { }
    Write-Host $line
}

function Add-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Detail = ''
    )
    $script:Steps += [PSCustomObject]@{ Name = $Name; Status = $Status; Detail = $Detail }
    Write-Log ('STEP {0}: {1} {2}' -f $Name, $Status, $Detail)
}

function Add-Error {
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $script:Errors += [PSCustomObject]@{ Stage = $Stage; Message = $Message }
    Write-Log ('ERROR [{0}] {1}' -f $Stage, $Message) 'ERROR'
}

function Show-Path {
    param([string]$Path)
    if (-not $Path) { return '' }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
        if ($full.StartsWith($script:Root, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $full.Substring($script:Root.Length).TrimStart([char[]]@('\', '/'))
        }
        return $full
    } catch { return $Path }
}

function Format-Rate {
    param($Value)
    if ($null -eq $Value) { return 'n/a' }
    try { return ('{0:N1}%' -f ([double]$Value * 100)) } catch { return 'n/a' }
}

function Get-Md5Hex {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm MD5).Hash.ToUpperInvariant()
    } catch {
        $md5 = [System.Security.Cryptography.MD5]::Create()
        try {
            $stream = [System.IO.File]::OpenRead($Path)
            try {
                return ([System.BitConverter]::ToString($md5.ComputeHash($stream))).Replace('-', '')
            } finally { $stream.Dispose() }
        } finally { $md5.Dispose() }
    }
}

function Read-TextFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Read-JsonFile {
    param([string]$Path)
    $text = Read-TextFile $Path
    if (-not $text) { return $null }
    try { return $text | ConvertFrom-Json } catch { return $null }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = '',
        [Parameter(Mandatory = $true)][string]$LogPath
    )
    $parent = Split-Path -Parent $LogPath
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $quoted = @()
    foreach ($item in $Arguments) {
        if ($item -match '[\s"]') { $quoted += ('"' + $item.Replace('"', '\"') + '"') }
        else { $quoted += $item }
    }
    $commandLine = ('& "{0}" {1}' -f $FilePath, ($quoted -join ' '))
    try {
        [System.IO.File]::AppendAllText(
            $script:CommandLog,
            ('[{0}] {1}  (cwd={2})' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $commandLine, $WorkingDirectory) + "`r`n",
            $script:Utf8NoBom
        )
    } catch { }

    $previous = (Get-Location).Path
    $code = -1
    try {
        if ($WorkingDirectory) { Set-Location -LiteralPath $WorkingDirectory }
        # 不用 *>：Windows PowerShell 5.1 的 *> 默认写 UTF-16LE，后面再追加
        # UTF-8 的 exit_code 会变成混合编码。统一用 Out-File -Encoding utf8。
        & $FilePath @Arguments 2>&1 | Out-File -LiteralPath $LogPath -Encoding utf8 -Width 4096
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } catch {
        $code = -1
        try {
            [System.IO.File]::AppendAllText($LogPath, "`r`n[wrapper-exception] " + $_.Exception.Message + "`r`n", $script:Utf8NoBom)
        } catch { }
    } finally {
        Set-Location -LiteralPath $previous
    }
    try {
        [System.IO.File]::AppendAllText($LogPath, "`r`n[exit_code] $code`r`n", $script:Utf8NoBom)
    } catch { }
    return $code
}

# ---------------------------------------------------------------------------
# 后台启动
# ---------------------------------------------------------------------------
if ((-not $Worker) -and (-not $NoBackground)) {
    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
    $psExe = $null
    try { $psExe = (Get-Process -Id $PID).Path } catch { }
    if (-not $psExe) { $psExe = Join-Path $PSHOME 'powershell.exe' }
    if (-not (Test-Path -LiteralPath $psExe)) {
        Write-Host "找不到 PowerShell 可执行文件: $psExe" -ForegroundColor Red
        exit 1
    }
    if (-not $script:ScriptPath) {
        Write-Host '无法确定脚本路径，不能后台启动。' -ForegroundColor Red
        exit 1
    }
    $stdout = Join-Path $script:LogDir 'console.out.txt'
    $stderr = Join-Path $script:LogDir 'console.err.txt'
    $argString = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Worker -Root "{1}" -Date {2}' -f $script:ScriptPath, $script:Root, $script:Date
    try {
        $startParams = @{
            FilePath = $psExe
            ArgumentList = $argString
            WorkingDirectory = $script:Root
            RedirectStandardOutput = $stdout
            RedirectStandardError = $stderr
            PassThru = $true
        }
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            $startParams['WindowStyle'] = 'Hidden'
        }
        $proc = Start-Process @startParams
    } catch {
        Write-Host ("后台启动失败: " + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
    $running = @(
        'WU validation started in background.',
        ('pid=' + $proc.Id),
        ('started_at=' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
        ('root=' + $script:Root),
        ('date=' + $script:Date),
        ('summary=' + $script:SummaryPath),
        ('log=' + $script:LogPath),
        ('console_out=' + $stdout),
        ('console_err=' + $stderr)
    ) -join "`r`n"
    try {
        [System.IO.File]::WriteAllText((Join-Path $script:LogDir 'RUNNING.txt'), $running + "`r`n", $script:Utf8NoBom)
    } catch { }
    Write-Host ''
    Write-Host '已在后台启动验证（隐藏窗口）。' -ForegroundColor Green
    Write-Host ('PID: ' + $proc.Id)
    Write-Host ('日志: ' + $script:LogPath)
    Write-Host ('摘要: ' + $script:SummaryPath)
    Write-Host ('进度: ' + (Join-Path $script:LogDir 'RUNNING.txt'))
    Write-Host '完成后会生成 DONE.txt；请勿关闭/强杀 msedge 进程。'
    exit 0
}

# ---------------------------------------------------------------------------
# Worker 正式执行
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
Write-Log '================================================================'
Write-Log ('WU validation worker start; root=' + $script:Root + '; date=' + $script:Date)
Write-Log ('PowerShell=' + $PSVersionTable.PSVersion.ToString() + '; OS=' + [System.Environment]::OSVersion.VersionString)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding -ArgumentList $false } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding -ArgumentList $false

# ---------------------------------------------------------------------------
# YAML：加“星星人”IP / 关闭导出
# ---------------------------------------------------------------------------
function Add-UltramanIp {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$WithReferenceImages
    )
    if (-not (Test-Path -LiteralPath $Path)) { throw "配置文件不存在: $Path" }
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ($text -match '(?m)^\s*-\s*code:\s*ultraman\s*$') {
        Write-Log ('配置已含 ultraman，跳过注入: ' + (Show-Path $Path))
        return
    }
    $newline = "`n"
    if ($text -match "`r`n") { $newline = "`r`n" }
    $match = [regex]::Match($text, '(?m)^ips:[ \t]*(?:#.*)?\r?\n')
    if (-not $match.Success) { throw "找不到顶层 ips: 节点: $Path" }
    $entry = @(
        '  - code: ultraman',
        '    name: 星星人',
        '    aliases: [迪迦, 赛罗, 泽塔, 银河, 艾克斯, 捷德, 特利迦, 德凯, 布莱泽, 赛文, Ultraman]',
        '    variants: []'
    )
    if ($WithReferenceImages) { $entry += '    reference_images: []' }
    $originalLine = $match.Value.TrimEnd("`r", "`n")
    $insert = $originalLine + $newline + ($entry -join $newline) + $newline
    $updated = $text.Substring(0, $match.Index) + $insert + $text.Substring($match.Index + $match.Length)
    [System.IO.File]::WriteAllText($Path, $updated, $script:Utf8NoBom)
    Write-Log ('已注入 ultraman IP: ' + (Show-Path $Path))
}

function Disable-ExportInRules {
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $pattern = '(?m)^(export:\s*\r?\n[ \t]+enabled:\s*)true([ \t]*(?:#.*)?)$'
    if ($text -match $pattern) {
        $updated = [regex]::Replace($text, $pattern, '${1}false${2}', 1)
        [System.IO.File]::WriteAllText($Path, $updated, $script:Utf8NoBom)
        Write-Log '已在临时 rules.yaml 关闭 export（避免写用户 Documents）'
        return $true
    }
    Write-Log '未找到 export.enabled: true，未改动 export 开关' 'WARN'
    return $false
}

function Disable-DetailFetchInRules {
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $before = $text
    $text = [regex]::Replace($text, '(?m)^(\s*detail_min_text_score:\s*)[0-9.]+', '${1}101')
    # 离线 dry-run 必须完全不触发京东临界带补抓：near_band_low=2.0、
    # near_band_high=1.0 组成空区间（low > high），不会匹配任何店铺金额，
    # 避免后端再启动浏览器/联网补抓详情。
    $text = [regex]::Replace($text, '(?m)^(\s*near_band_low:\s*)[0-9.]+', '${1}2.0')
    $text = [regex]::Replace($text, '(?m)^(\s*near_band_high:\s*)[0-9.]+', '${1}1.0')
    if ($text -ne $before) {
        [System.IO.File]::WriteAllText($Path, $text, $script:Utf8NoBom)
        Write-Log '已在临时 rules.yaml 关闭详情补抓（detail_min_text_score=101，京东临界带 2.0-1.0 空区间）'
        return $true
    }
    Write-Log '未找到详情补抓阈值，未改动 fetch 配置' 'WARN'
    return $false
}

function Configure-TempPathsInRules {
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $before = $text
    $logs = (Join-Path $script:LogDir 'backend_logs').Replace('\', '/')
    $images = (Join-Path $script:LogDir 'backend_images').Replace('\', '/')
    $backups = (Join-Path $script:LogDir 'backend_backups').Replace('\', '/')
    $lock = (Join-Path $script:LogDir 'pipeline.lock').Replace('\', '/')
    $text = [regex]::Replace($text, '(?m)^(\s*logs:\s*).*$', ('${1}"' + $logs + '"'))
    $text = [regex]::Replace($text, '(?m)^(\s*images:\s*).*$', ('${1}"' + $images + '"'))
    $text = [regex]::Replace($text, '(?m)^(\s*backups:\s*).*$', ('${1}"' + $backups + '"'))
    $text = [regex]::Replace($text, '(?m)^(\s*lock_file:\s*).*$', ('${1}"' + $lock + '"'))
    if ($text -ne $before) {
        [System.IO.File]::WriteAllText($Path, $text, $script:Utf8NoBom)
        Write-Log ('已把 backend 日志/图片/备份/锁重定向到: ' + (Show-Path $script:LogDir))
        return $true
    }
    Write-Log '未找到 backend 路径键，未改动 paths/run.lock_file' 'WARN'
    return $false
}

# ---------------------------------------------------------------------------
# 备份 + 解压
# ---------------------------------------------------------------------------
function Expand-ZipToRoot {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $rootFull = [System.IO.Path]::GetFullPath($Destination)
    $separator = [System.IO.Path]::DirectorySeparatorChar
    if (-not $rootFull.EndsWith([string]$separator)) { $rootFull += $separator }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $archive.Entries) {
            $relative = $entry.FullName
            if (-not $relative) { continue }
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination $relative))
            if (-not $target.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw ('zip 条目越界: ' + $relative)
            }
            if ($entry.Name -eq '') {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
                continue
            }
            if ($script:ScriptPath -and ($target -ieq [System.IO.Path]::GetFullPath($script:ScriptPath))) {
                Write-Log ('跳过覆盖正在运行的脚本: ' + $relative)
                continue
            }
            $parent = Split-Path -Parent $target
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally {
        $archive.Dispose()
    }
}

function Backup-AndExtract {
    $zip = $script:PackageZipPath
    if (-not (Test-Path -LiteralPath $zip)) {
        throw ('验证包不存在: ' + $zip)
    }
    $backup = $script:BackupDir
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    foreach ($relative in @('backend\config\rules.yaml', 'crawler\config\windows.yaml')) {
        $source = Join-Path $script:Root $relative
        if (Test-Path -LiteralPath $source) {
            $target = Join-Path $backup $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $source -Destination $target -Force
            Write-Log ('已备份 ' + $relative + ' -> ' + (Show-Path $target))
        } else {
            Write-Log ('待备份配置不存在，跳过: ' + $relative) 'WARN'
        }
    }

    $robocopy = Get-Command robocopy.exe -ErrorAction SilentlyContinue
    if (-not $robocopy) { $robocopy = Get-Command robocopy -ErrorAction SilentlyContinue }
    $robocopyPath = $null
    if ($robocopy) { $robocopyPath = [string]$robocopy.Source }
    foreach ($module in @('crawler', 'backend', 'export')) {
        $source = Join-Path $script:Root $module
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $target = Join-Path $backup ('code\' + $module)
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        if ($robocopyPath) {
            & $robocopyPath $source $target /E /XD .venv .deps out logs state data profiles __pycache__ .pytest_cache backups /XF *.log .coverage /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
            $code = $LASTEXITCODE
            if ($code -ge 8) {
                throw ('robocopy 备份失败 module=' + $module + ' exit=' + $code)
            }
            Write-Log ('已备份代码 ' + $module + ' -> ' + (Show-Path $target) + ' (robocopy exit=' + $code + ')')
        } else {
            Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
            Write-Log ('已备份代码 ' + $module + ' -> ' + (Show-Path $target) + ' (Copy-Item)')
        }
    }
    Add-Step '备份' 'OK' ($script:BackupRel)

    Write-Log ('解压覆盖: ' + (Show-Path $zip) + ' -> ' + $script:Root)
    Expand-ZipToRoot -ZipPath $zip -Destination $script:Root
    foreach ($required in @('crawler\config\windows.yaml', 'backend\config\rules.yaml', 'wu_validate.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $script:Root $required))) {
            throw ('解压后缺少: ' + $required)
        }
    }
    Add-Step '解压' 'OK' (Show-Path $zip)
}

# ---------------------------------------------------------------------------
# 依赖与测试
# ---------------------------------------------------------------------------
function Get-ModulePython {
    param([string]$Module)
    $candidate = Join-Path $script:Root ($Module + '\.venv\Scripts\python.exe')
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function New-BackendVenv {
    param([string]$CrawlerPython)
    $backendDir = Join-Path $script:Root 'backend'
    $venvDir = Join-Path $backendDir '.venv'
    $venvPy = Join-Path $venvDir 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) { return $venvPy }
    if (-not $CrawlerPython -or -not (Test-Path -LiteralPath $CrawlerPython)) {
        Write-Log '没有 crawler\.venv python，无法创建 backend\.venv' 'WARN'
        return $null
    }
    $log = Join-Path $script:LogDir 'backend_venv_create.txt'
    Write-Log ('backend\.venv 不存在，用 crawler python 同版本创建: ' + $CrawlerPython + ' -m venv ' + $venvDir)
    $code = Invoke-External -Label 'venv:backend' -FilePath $CrawlerPython -Arguments @('-m', 'venv', $venvDir) -WorkingDirectory $backendDir -LogPath $log
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) {
        Write-Log ('创建 backend\.venv 失败 exit=' + $code + '，详见 ' + (Show-Path $log)) 'WARN'
        return $null
    }
    Add-Step 'backend venv' 'OK' ('已用 crawler python 创建: ' + (Show-Path $venvDir))
    return $venvPy
}

function Enable-BackendFallback {
    param(
        [string]$Reason,
        [string]$CrawlerPython
    )
    $script:BackendFallback = $true
    $script:BackendPythonSource = 'crawler-fallback'
    $script:BackendNote = $Reason
    $script:BackendPython = $CrawlerPython
    Write-Log $Reason 'WARN'
    Add-Step 'backend venv' 'WARN' $Reason
    return $CrawlerPython
}

function Resolve-BackendPython {
    param([string]$CrawlerPython)
    $existing = Get-ModulePython 'backend'
    if ($existing) {
        $script:BackendPythonSource = 'backend-venv'
        return $existing
    }
    $created = New-BackendVenv -CrawlerPython $CrawlerPython
    if ($created) {
        $script:BackendPythonSource = 'backend-venv-new'
        return $created
    }
    $reason = 'backend\.venv 不存在且创建失败，已回退 crawler\.venv 跑 backend pytest 与打分（SUMMARY 已标注）'
    return (Enable-BackendFallback -Reason $reason -CrawlerPython $CrawlerPython)
}

function Install-Requirements {
    param(
        [string]$Label,
        [string]$Python,
        [string]$Requirements,
        [string]$LogName,
        [switch]$Soft
    )
    if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
        if ($Soft) {
            Write-Log ('找不到 venv python，跳过依赖安装: ' + $Label + ' ' + $Python) 'WARN'
        } else {
            Add-Error ('pip:' + $Label) ('找不到 venv python: ' + $Python)
        }
        return $false
    }
    if (-not (Test-Path -LiteralPath $Requirements)) {
        if ($Soft) {
            Write-Log ('找不到 requirements，跳过依赖安装: ' + $Label + ' ' + $Requirements) 'WARN'
        } else {
            Add-Error ('pip:' + $Label) ('找不到 requirements: ' + $Requirements)
        }
        return $false
    }
    $log = Join-Path $script:LogDir $LogName
    $cwd = Split-Path -Parent $Requirements
    $code = Invoke-External -Label ('pip:' + $Label) -FilePath $Python -Arguments @('-m', 'pip', 'install', '-r', $Requirements, '--quiet', '--disable-pip-version-check', '--no-input') -WorkingDirectory $cwd -LogPath $log
    if ($code -ne 0) {
        if ($Soft) {
            Write-Log ('pip install 失败 exit=' + $code + '，将尝试回退: ' + $Label + '，详见 ' + (Show-Path $log)) 'WARN'
        } else {
            Add-Error ('pip:' + $Label) ('pip install 失败 exit=' + $code + '，详见 ' + (Show-Path $log))
        }
        return $false
    }
    Add-Step ('依赖:' + $Label) 'OK' (Show-Path $log)
    return $true
}

function Run-Pytest {
    param(
        [string]$Label,
        [string]$Python,
        [string]$WorkingDirectory,
        [string]$LogName
    )
    if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
        Add-Error ('pytest:' + $Label) ('找不到 venv python: ' + $Python)
        $script:PytestResults[$Label] = -1
        return -1
    }
    $log = Join-Path $script:LogDir $LogName
    $code = Invoke-External -Label ('pytest:' + $Label) -FilePath $Python -Arguments @('-m', 'pytest', '-q') -WorkingDirectory $WorkingDirectory -LogPath $log
    $script:PytestResults[$Label] = $code
    if ($code -eq 0) {
        Add-Step ('pytest:' + $Label) 'PASS' (Show-Path $log)
    } else {
        Add-Error ('pytest:' + $Label) ('pytest 失败 exit=' + $code + '，详见 ' + (Show-Path $log))
    }
    return $code
}

# ---------------------------------------------------------------------------
# CDP：探测 + 优雅关闭（绝不 taskkill / Stop-Process）
# ---------------------------------------------------------------------------
function Test-CdpPort {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:' + $Port + '/json/version') -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch { return $false }
}

function Close-CdpBrowser {
    param([int]$Port)
    # 11/6 修复：PowerShell 5.1 的 Add-Type System.Net.WebSockets.Client 在 WU
    # 会报“无法添加类型。缺少一个或多个所需程序集”。改为调用 crawler\.venv
    # 的 python（wu_tools\cdp_close.py，纯标准库）发 CDP Browser.close。
    # 只对本脚本自己启动的端口调用；绝不 taskkill / Stop-Process。
    $crawlerPy = Get-ModulePython 'crawler'
    if (-not $crawlerPy) {
        Write-Log ('crawler\.venv python 不存在，无法通过 CDP 关闭 Edge（保留进程）: port=' + $Port) 'WARN'
        return $false
    }
    $closeScript = Join-Path $script:Root 'wu_tools\cdp_close.py'
    if (-not (Test-Path -LiteralPath $closeScript)) {
        Write-Log ('wu_tools\cdp_close.py 不存在，无法通过 CDP 关闭 Edge（保留进程）: port=' + $Port) 'WARN'
        return $false
    }
    $log = Join-Path $script:LogDir ('cdp_close_' + $Port + '.txt')
    Write-Log ('发送 CDP Browser.close: port=' + $Port + ' python=' + $crawlerPy)
    $code = Invoke-External -Label ('cdp-close:' + $Port) -FilePath $crawlerPy -Arguments @($closeScript, '--port', [string]$Port, '--timeout', '5', '--wait', '10') -WorkingDirectory $script:Root -LogPath $log
    if ($code -eq 0) {
        Write-Log ('Edge 已通过 CDP Browser.close 正常退出: port=' + $Port)
        return $true
    }
    if ($code -eq 3) {
        Write-Log ('CDP 端口已不可用（视为已退出）: port=' + $Port)
        return $true
    }
    if ($code -eq 1) {
        Write-Log ('已发送 Browser.close，但端口仍监听（保留，不强杀）: port=' + $Port) 'WARN'
        return $false
    }
    Write-Log ('优雅关闭 Edge 失败（保留进程，不强杀）: port=' + $Port + ' exit=' + $code + '，详见 ' + (Show-Path $log)) 'WARN'
    return $false
}

# ---------------------------------------------------------------------------
# 小量采集
# ---------------------------------------------------------------------------
function Get-JsonlRows {
    param([string]$Path, [int]$Max = 0)
    $rows = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $rows }
    $reader = New-Object System.IO.StreamReader($Path, [System.Text.Encoding]::UTF8)
    try {
        while ($null -ne ($line = $reader.ReadLine())) {
            $text = $line.Trim()
            if (-not $text) { continue }
            try { $rows += ($text | ConvertFrom-Json) } catch { }
            if ($Max -gt 0 -and $rows.Count -ge $Max) { break }
        }
    } finally { $reader.Dispose() }
    return $rows
}

function Invoke-CrawlPhase {
    $crawlerPy = Get-ModulePython 'crawler'
    if (-not $crawlerPy) {
        Add-Error 'crawl' 'crawler\.venv\Scripts\python.exe 不存在，跳过采集'
        return
    }
    $crawlerDir = Join-Path $script:Root 'crawler'
    $windows = $script:WindowsPath
    if (-not (Test-Path -LiteralPath $windows)) {
        Add-Error 'crawl' ('windows.yaml 不存在: ' + $windows)
        return
    }
    $crawlConfig = Join-Path $script:LogDir 'windows_ultraman.yaml'
    Copy-Item -LiteralPath $windows -Destination $crawlConfig -Force
    try { Add-UltramanIp -Path $crawlConfig } catch {
        Add-Error 'crawl' ('临时采集配置注入 ultraman 失败: ' + $_.Exception.Message)
        return
    }

    $crawlOut = Join-Path $script:Root $script:OutRel
    $crawlLogs = Join-Path $script:LogDir 'crawler_logs'
    $crawlState = Join-Path $script:LogDir 'state'
    New-Item -ItemType Directory -Force -Path $crawlOut, $crawlLogs, $crawlState | Out-Null

    $oldOut = $env:CRAWLER_OUTPUT_DIR
    $oldLog = $env:CRAWLER_LOG_DIR
    $oldState = $env:CRAWLER_STATE_DIR
    $env:CRAWLER_OUTPUT_DIR = $crawlOut
    $env:CRAWLER_LOG_DIR = $crawlLogs
    $env:CRAWLER_STATE_DIR = $crawlState

    $ports = @{ 'jd' = 9223; '1688' = 9224; 'taobao' = 9222 }
    try {
        foreach ($platform in @('jd', '1688', 'taobao')) {
            $port = [int]$ports[$platform]
            $preOpen = Test-CdpPort -Port $port
            Write-Log ('采集开始 platform=' + $platform + ' port=' + $port + ' preOpen=' + $preOpen)
            $runLog = Join-Path $script:LogDir ('crawl_' + $platform + '.txt')
            $code = Invoke-External -Label ('crawl:' + $platform) -FilePath $crawlerPy -Arguments @(
                '-m', 'crawler', 'run',
                '--platform', $platform,
                '--date', $script:Date,
                '--keyword', '星星人',
                '--max-pages', '1',
                '--config', $crawlConfig
            ) -WorkingDirectory $crawlerDir -LogPath $runLog

            $runDir = Join-Path $crawlOut $script:Date
            $runJson = Join-Path $runDir ($platform + '.run.json')
            $record = Read-JsonFile $runJson
            if ($record) {
                Copy-Item -LiteralPath $runJson -Destination (Join-Path $script:LogDir ('list_' + $platform + '.run.json')) -Force
            }
            $status = 'unknown'
            $items = 0
            $risk = $false
            $loginOk = $false
            $errorsText = ''
            if ($record) {
                $status = [string]$record.status
                if ($null -ne $record.items_ok) { $items = [int]$record.items_ok }
                if ($null -ne $record.risk_blocked) { $risk = [bool]$record.risk_blocked }
                if ($null -ne $record.login_ok) { $loginOk = [bool]$record.login_ok }
                if ($record.errors) { $errorsText = (@($record.errors) -join '；') }
            }
            $browserStarted = $false
            if ($record -and $null -ne $record.browser_started) {
                $browserStarted = [bool]$record.browser_started
            }
            $detailStatus = 'skipped'
            $detailCount = 0
            if ($status -eq 'risk_blocked') {
                Write-Log ('风控跳过 platform=' + $platform + ' reason=' + $errorsText) 'WARN'
            } elseif ($status -eq 'ok' -and $items -gt 0) {
                if ($platform -eq '1688') {
                    # 11/6 轮：1688 完整详情默认停用（配置 crawl.detail.platforms.1688=false），
                    # 列表照采；仅对命中关键词且缺店名的候选按配置上限补抓店名。
                    $detailStatus = 'disabled'
                    Write-Log '1688 完整详情本轮停用（配置开关 detail.platforms.1688=false），列表照常采集；缺店名候选按上限补抓' 'WARN'
                } else {
                    $jsonl = Join-Path $runDir ($platform + '.jsonl')
                    $rows = Get-JsonlRows -Path $jsonl
                        $ids = @()
                        $seen = @{}
                        foreach ($row in $rows) {
                            $pidValue = [string]$row.product_id
                            if (-not $pidValue) { continue }
                            $rowPlatform = [string]$row.platform
                            if ($platform -eq 'taobao' -and $rowPlatform -eq 'tmall') {
                                $pidValue = 'tmall:' + $pidValue
                            }
                            if ($seen.ContainsKey($pidValue)) { continue }
                            $seen[$pidValue] = $true
                            $ids += $pidValue
                            if ($ids.Count -ge 5) { break }
                        }
                        if ($ids.Count -gt 0) {
                            $detailLog = Join-Path $script:LogDir ('detail_' + $platform + '.txt')
                            Write-Log ('详情补抓 platform=' + $platform + ' ids=' + ($ids -join ','))
                            $detailCode = Invoke-External -Label ('detail:' + $platform) -FilePath $crawlerPy -Arguments @(
                                '-m', 'crawler', 'detail',
                                '--platform', $platform,
                                '--date', $script:Date,
                                '--ids', ($ids -join ','),
                                '--config', $crawlConfig
                            ) -WorkingDirectory $crawlerDir -LogPath $detailLog
                            $detailRecord = Read-JsonFile $runJson
                            if ($detailRecord) {
                                Copy-Item -LiteralPath $runJson -Destination (Join-Path $script:LogDir ('detail_' + $platform + '.run.json')) -Force
                                $detailStatus = [string]$detailRecord.status
                                if ($null -ne $detailRecord.items_ok) { $detailCount = [int]$detailRecord.items_ok }
                            } else {
                                $detailStatus = 'unknown'
                            }
                            if ($detailCode -ne 0 -and $detailStatus -eq 'ok') { $detailStatus = 'exit_' + $detailCode }
                            if ($detailStatus -eq 'risk_blocked') {
                                Write-Log ('详情页风控 platform=' + $platform + '，停止该平台详情') 'WARN'
                            }
                        }
                }
            } else {
                Write-Log ('平台无列表数据，跳过详情 platform=' + $platform + ' status=' + $status) 'WARN'
            }

            # 只关闭“运行前端口未开 + 采集端明确记录自己启动了浏览器 + 现在还在监听”
            # 的实例；用户自己开的 Edge（preOpen=true / browser_started=false）绝不碰。
            $started = ((-not $preOpen) -and $browserStarted -and (Test-CdpPort -Port $port))
            if ($started) {
                $closed = Close-CdpBrowser -Port $port
                $script:CloseResults += [PSCustomObject]@{ Platform = $platform; Port = $port; Started = $true; Closed = $closed }
            } else {
                $script:CloseResults += [PSCustomObject]@{ Platform = $platform; Port = $port; Started = $false; Closed = $false }
            }

            $script:CrawlResults += [PSCustomObject]@{
                Platform = $platform
                ExitCode = $code
                Status = $status
                Items = $items
                LoginOk = $loginOk
                RiskBlocked = $risk
                DetailStatus = $detailStatus
                DetailCount = $detailCount
                Errors = $errorsText
                RunJson = Show-Path $runJson
                Log = Show-Path $runLog
            }
            if ($status -notin @('ok', 'risk_blocked', 'disabled')) {
                Add-Error ('crawl:' + $platform) ('status=' + $status + ' exit=' + $code + ' items=' + $items + ' ' + $errorsText)
            } elseif ($code -ne 0 -and $status -eq 'ok') {
                Add-Error ('crawl:' + $platform) ('crawler 退出码 ' + $code + '（status=ok），详见 ' + (Show-Path $runLog))
            }
            if ($detailStatus -notin @('ok', 'skipped', 'risk_blocked', 'disabled') -and $detailStatus -ne '') {
                Add-Error ('detail:' + $platform) ('detail status=' + $detailStatus + '，详见 ' + (Show-Path (Join-Path $script:LogDir ('detail_' + $platform + '.txt'))))
            }
            Add-Step ('采集:' + $platform) $(if ($status -eq 'ok') { 'OK' } else { 'WARN' }) ('status=' + $status + ' items=' + $items + ' detail=' + $detailStatus + '/' + $detailCount)
        }

        # 收尾：对本次启动但尚未退出的 Edge 再优雅关闭一次；仍失败则保留。
        foreach ($item in $script:CloseResults) {
            if ($item.Started -and (-not $item.Closed) -and (Test-CdpPort -Port $item.Port)) {
                Write-Log ('收尾重试优雅关闭 Edge: platform=' + $item.Platform + ' port=' + $item.Port)
                $retry = Close-CdpBrowser -Port $item.Port
                if ($retry) { $item.Closed = $true }
            }
        }
    } finally {
        if ($null -eq $oldOut) { Remove-Item Env:CRAWLER_OUTPUT_DIR -ErrorAction SilentlyContinue } else { $env:CRAWLER_OUTPUT_DIR = $oldOut }
        if ($null -eq $oldLog) { Remove-Item Env:CRAWLER_LOG_DIR -ErrorAction SilentlyContinue } else { $env:CRAWLER_LOG_DIR = $oldLog }
        if ($null -eq $oldState) { Remove-Item Env:CRAWLER_STATE_DIR -ErrorAction SilentlyContinue } else { $env:CRAWLER_STATE_DIR = $oldState }
    }
}

# ---------------------------------------------------------------------------
# run-entry 非交互冒烟：临时 whitelist 目录，不碰正式 whitelist\
# ---------------------------------------------------------------------------
function Invoke-RunEntrySmoke {
    $py = $script:BackendPython
    if (-not $py) { $py = Get-ModulePython 'backend' }
    if (-not $py) { $py = Get-ModulePython 'crawler' }
    if (-not $py) {
        Add-Error 'run_entry' '没有可用的 Python，跳过非交互 run-entry'
        return
    }
    $whitelistDir = Join-Path $script:LogDir 'whitelist'
    $configDir = Join-Path $script:LogDir 'run_entry_config'
    $log = Join-Path $script:LogDir 'run_entry.txt'
    $code = Invoke-External -Label 'run_entry' -FilePath $py -Arguments @(
        '-m', 'pipeline', 'run-entry',
        '--ip', '星星人',
        '--keywords', '星星人',
        '--whitelist-dir', $whitelistDir,
        '--output-dir', $configDir,
        '--prepare-only',
        '--no-editor',
        '--config', $script:RulesPath
    ) -WorkingDirectory (Join-Path $script:Root 'backend') -LogPath $log
    if ($code -ne 0) {
        Add-Error 'run_entry' ('非交互 run-entry 失败 exit=' + $code + '，详见 ' + (Show-Path $log))
        return
    }
    Add-Step 'run-entry 非交互' 'OK' ('临时白名单=' + (Show-Path $whitelistDir) + ' 配置=' + (Show-Path $configDir))
}

# ---------------------------------------------------------------------------
# trial_metrics
# ---------------------------------------------------------------------------
function Invoke-TrialMetrics {
    $crawlerPy = Get-ModulePython 'crawler'
    if (-not $crawlerPy) {
        Add-Error 'trial_metrics' 'crawler venv 不存在，跳过'
        return
    }
    $inputDir = Join-Path $script:Root ($script:OutRel + '\' + $script:Date)
    if (-not (Test-Path -LiteralPath $inputDir)) {
        Add-Error 'trial_metrics' ('采集输出目录不存在: ' + (Show-Path $inputDir))
        return
    }
    $jsonOut = Join-Path $script:LogDir 'trial_metrics.json'
    $mdOut = Join-Path $script:LogDir 'trial_metrics.md'
    $log = Join-Path $script:LogDir 'trial_metrics_console.txt'
    $metricsArgs = @(
        'tools/trial_metrics.py',
        '--input-dir', $inputDir,
        '--json-out', $jsonOut,
        '--md-out', $mdOut
    )
    $code = Invoke-External -Label 'trial_metrics' -FilePath $crawlerPy -Arguments $metricsArgs -WorkingDirectory (Join-Path $script:Root 'crawler') -LogPath $log
    if ($code -ne 0) {
        Add-Error 'trial_metrics' ('trial_metrics 失败 exit=' + $code + '，详见 ' + (Show-Path $log))
        return
    }
    $script:TrialMetrics = Read-JsonFile $jsonOut
    Add-Step 'trial_metrics' 'OK' ((Show-Path $jsonOut) + ' / ' + (Show-Path $mdOut))
}

# ---------------------------------------------------------------------------
# 星星人打分（临时改 yaml -> dry-run -> 统计 -> 还原 + MD5）
# ---------------------------------------------------------------------------
function Invoke-UltramanScoring {
    $backendPy = $script:BackendPython
    if (-not $backendPy) { $backendPy = Get-ModulePython 'backend' }
    if (-not $backendPy) { $backendPy = Get-ModulePython 'crawler' }
    if (-not $backendPy) {
        Add-Error 'ultraman' 'backend\.venv 与 crawler\.venv 都不存在，跳过打分'
        return
    }
    if ($script:BackendFallback) {
        Write-Log ('打分使用回退环境 crawler\.venv（' + (Show-Path $backendPy) + '）') 'WARN'
    }
    $rules = $script:RulesPath
    $windows = $script:WindowsPath
    if (-not (Test-Path -LiteralPath $rules) -or -not (Test-Path -LiteralPath $windows)) {
        Add-Error 'ultraman' 'rules.yaml 或 windows.yaml 不存在，跳过打分'
        return
    }
    $rulesBak = $rules + '.bak'
    $windowsBak = $windows + '.bak'
    $rulesMd5Before = Get-Md5Hex $rules
    $windowsMd5Before = Get-Md5Hex $windows
    Copy-Item -LiteralPath $rules -Destination $rulesBak -Force
    Copy-Item -LiteralPath $windows -Destination $windowsBak -Force
    Write-Log ('已创建 .bak: ' + (Show-Path $rulesBak) + ' / ' + (Show-Path $windowsBak))

    try {
        Add-UltramanIp -Path $rules -WithReferenceImages
        Add-UltramanIp -Path $windows
        Disable-ExportInRules -Path $rules | Out-Null
        Disable-DetailFetchInRules -Path $rules | Out-Null
        Configure-TempPathsInRules -Path $rules | Out-Null

        $source1 = Join-Path $script:Root ($script:OutRel + '\' + $script:Date)
        $source2 = Join-Path $script:Root 'crawler\out\2026-11-05'
        if (-not (Test-Path -LiteralPath $source2)) {
            $fallback = Join-Path $script:Root 'crawler\samples\2026-11-05'
            if (Test-Path -LiteralPath $fallback) {
                Write-Log ('2026-11-05 实采数据不存在，回退到 samples: ' + (Show-Path $fallback)) 'WARN'
                $source2 = $fallback
            } else {
                Add-Error 'ultraman' '既无 crawler\out\2026-11-05 也无 samples\2026-11-05'
            }
        }
        if (-not (Test-Path -LiteralPath $source1)) {
            Add-Error 'ultraman' ('本次采集输出不存在: ' + (Show-Path $source1))
        }

        $staging = Join-Path $script:LogDir 'staging'
        $manifest = Join-Path $script:LogDir 'staging_manifest.json'
        $stageLog = Join-Path $script:LogDir 'staging_console.txt'
        $stageArgs = @((Join-Path $script:Root 'wu_tools\build_staging.py'), '--out', $staging, '--manifest', $manifest)
        if (Test-Path -LiteralPath $source1) { $stageArgs += @('--source', $source1) }
        if (Test-Path -LiteralPath $source2) { $stageArgs += @('--source', $source2) }
        $stageCode = Invoke-External -Label 'staging' -FilePath $backendPy -Arguments $stageArgs -WorkingDirectory $script:Root -LogPath $stageLog
        if ($stageCode -ne 0) {
            Add-Error 'ultraman' ('staging 合并失败 exit=' + $stageCode + '，详见 ' + (Show-Path $stageLog))
            return
        }

        $dryRoot = Join-Path $script:LogDir 'ultraman_dryrun'
        if (Test-Path -LiteralPath $dryRoot) {
            $resolvedDry = [System.IO.Path]::GetFullPath($dryRoot)
            if ($resolvedDry.StartsWith($script:LogDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $dryRoot -Recurse -Force
            } else {
                throw ('拒绝清理非日志目录: ' + $resolvedDry)
            }
        }
        New-Item -ItemType Directory -Force -Path $dryRoot | Out-Null
        $oldKeep = $env:WU_KEEP_DRYRUN_DIR
        $env:WU_KEEP_DRYRUN_DIR = $dryRoot
        try {
            $pipelineLog = Join-Path $script:LogDir 'ultraman_pipeline.txt'
            $pipelineCode = Invoke-External -Label 'ultraman-pipeline' -FilePath $backendPy -Arguments @(
                (Join-Path $script:Root 'wu_tools\run_pipeline_keepdb.py'),
                '--config', $rules,
                'run',
                '--input-dir', $staging,
                '--date', $script:Date,
                '--dry-run',
                '--no-alert'
            ) -WorkingDirectory (Join-Path $script:Root 'backend') -LogPath $pipelineLog
        } finally {
            if ($null -eq $oldKeep) { Remove-Item Env:WU_KEEP_DRYRUN_DIR -ErrorAction SilentlyContinue } else { $env:WU_KEEP_DRYRUN_DIR = $oldKeep }
        }

        $db = $null
        $dbFile = Get-ChildItem -LiteralPath $dryRoot -Recurse -Filter 'leads.db' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($dbFile) { $db = $dbFile.FullName }
        if (-not $db) {
            Add-Error 'ultraman' ('dry-run 未找到临时 leads.db（exit=' + $pipelineCode + '），详见 ' + (Show-Path $pipelineLog))
            return
        }
        Write-Log ('dry-run 临时库: ' + (Show-Path $db) + ' (pipeline exit=' + $pipelineCode + ')')

        $reportMd = Join-Path $script:LogDir 'ultraman_score.md'
        $reportJson = Join-Path $script:LogDir 'ultraman_score.json'
        $reportLog = Join-Path $script:LogDir 'ultraman_report_console.txt'
        $reportCode = Invoke-External -Label 'ultraman-report' -FilePath $backendPy -Arguments @(
            (Join-Path $script:Root 'wu_tools\ultraman_report.py'),
            '--db', $db,
            '--out-md', $reportMd,
            '--out-json', $reportJson,
            '--samples', '10',
            '--run-date', $script:Date
        ) -WorkingDirectory $script:Root -LogPath $reportLog
        if ($reportCode -ne 0) {
            Add-Error 'ultraman' ('统计失败 exit=' + $reportCode + '，详见 ' + (Show-Path $reportLog))
            return
        }
        $script:Ultraman = Read-JsonFile $reportJson
        if (-not $script:Ultraman) {
            Add-Error 'ultraman' ('无法读取统计 JSON: ' + (Show-Path $reportJson))
            return
        }
        $script:Ultraman | Add-Member -NotePropertyName 'pipeline_exit' -NotePropertyValue $pipelineCode -Force
        $script:Ultraman | Add-Member -NotePropertyName 'db' -NotePropertyValue (Show-Path $db) -Force
        $script:Ultraman | Add-Member -NotePropertyName 'report_md' -NotePropertyValue (Show-Path $reportMd) -Force
        if ($pipelineCode -ne 0) {
            Add-Error 'ultraman' ('pipeline 退出码 ' + $pipelineCode + '，详见 ' + (Show-Path $pipelineLog) + '；统计仍已生成')
        }
        Add-Step '星星人打分' 'OK' ((Show-Path $reportMd) + ' total=' + $script:Ultraman.total + ' candidates=' + $script:Ultraman.candidates)
    } finally {
        foreach ($pair in @(
            @{ Path = $rules; Bak = $rulesBak; Name = 'backend\rules.yaml'; Before = $rulesMd5Before },
            @{ Path = $windows; Bak = $windowsBak; Name = 'crawler\windows.yaml'; Before = $windowsMd5Before }
        )) {
            $restored = $false
            $after = $null
            try {
                if (Test-Path -LiteralPath $pair.Bak) {
                    Copy-Item -LiteralPath $pair.Bak -Destination $pair.Path -Force
                    $restored = $true
                    $after = Get-Md5Hex $pair.Path
                }
            } catch {
                Add-Error 'restore' ($pair.Name + ' 还原失败: ' + $_.Exception.Message)
            }
            $match = ($restored -and $after -and $pair.Before -and ($after -eq $pair.Before))
            if (-not $match) {
                Add-Error 'restore' ($pair.Name + ' MD5 校验失败 before=' + $pair.Before + ' after=' + $after)
            }
            $script:RestoreResults += [PSCustomObject]@{
                File = $pair.Name
                Restored = $restored
                Md5Before = $pair.Before
                Md5After = $after
                Match = [bool]$match
            }
            if ($match) {
                Write-Log ('还原并 MD5 校验通过: ' + $pair.Name + ' ' + $after)
            }
        }
        $allMatch = $true
        foreach ($item in $script:RestoreResults) { if (-not $item.Match) { $allMatch = $false } }
        Add-Step '配置还原' $(if ($allMatch) { 'PASS' } else { 'FAIL' }) ('MD5 ' + $(if ($allMatch) { '一致' } else { '不一致' }))
    }
}

# ---------------------------------------------------------------------------
# SUMMARY / DONE
# ---------------------------------------------------------------------------
function Write-SummaryAndDone {
    $finished = Get-Date
    $duration = [int]($finished - $script:StartedAt).TotalSeconds
    $crawlOk = 0
    foreach ($item in $script:CrawlResults) {
        if ($item.Status -eq 'ok' -and $item.Items -gt 0) { $crawlOk++ }
    }
    $pytestOk = $true
    foreach ($key in $script:PytestResults.Keys) {
        if ([int]$script:PytestResults[$key] -ne 0) { $pytestOk = $false }
    }
    $restoreOk = ($script:RestoreResults.Count -gt 0)
    foreach ($item in $script:RestoreResults) { if (-not $item.Match) { $restoreOk = $false } }

    $status = 'PASS'
    if ($script:FatalError) {
        $status = 'FAIL'
    } elseif ($script:Errors.Count -gt 0) {
        $status = 'PARTIAL'
    }
    if (-not $script:FatalError -and $crawlOk -eq 0) { $status = 'PARTIAL' }
    if ($script:RestoreResults.Count -gt 0 -and -not $restoreOk) { $status = 'FAIL' }
    if (-not $script:FatalError -and $script:CrawlResults.Count -eq 0) { $status = 'PARTIAL' }
    if (-not $pytestOk -and $status -eq 'PASS') { $status = 'PARTIAL' }

    $lines = @()
    $lines += ('# WU 一次性验证摘要（' + $script:TitleVersion + '）')
    $lines += ''
    $lines += ('- 结论：**' + $status + '**')
    $lines += ('- 开始：' + $script:StartedAt.ToString('yyyy-MM-dd HH:mm:ss'))
    $lines += ('- 结束：' + $finished.ToString('yyyy-MM-dd HH:mm:ss') + '（' + $duration + ' 秒）')
    $lines += ('- 根目录：`' + $script:Root + '`')
    $lines += ('- 验证日期：`' + $script:Date + '`')
    $lines += ('- PowerShell：' + $PSVersionTable.PSVersion.ToString())
    $lines += ('- Python（crawler）：' + $(if (Get-ModulePython 'crawler') { '有' } else { '缺' }))
    $lines += ('- Python（backend）：' + $(if ($script:BackendPython) { (Show-Path $script:BackendPython) } else { '缺' }))
    $lines += ''
    $lines += '## 环境与开关'
    $lines += ''
    $lines += ('- backend python 来源：`' + $script:BackendPythonSource + '`')
    if ($script:BackendFallback) {
        $lines += ('- **backend\.venv 回退**：' + $script:BackendNote)
    } elseif ($script:BackendPythonSource -eq 'backend-venv-new') {
        $lines += '- backend\.venv 由 crawler\.venv 同版本 python 创建并安装依赖。'
    } else {
        $lines += '- backend\.venv 原已存在，未重建。'
    }
    $lines += '- 调用 crawler 的子进程 Python：优先 `crawler\.venv\Scripts\python.exe`（不存在才回退当前解释器），避免 backend venv 缺 pydantic。'
    $lines += '- **1688 完整详情本轮停用**（`crawl.detail.platforms.1688=false`）：1688 列表照采，不进入详情页补评论数/店铺 ID；仅对命中关键词且缺店名的候选补抓店名（`crawl.ali1688.shop_name_backfill`，每次运行上限 30，复用详情节流）。'
    $lines += '- 淘宝/1688 缺评论数照常标记 `评论缺失`；不会用京东评论数回填。'
    $lines += ''
    if ($script:FatalError) {
        $lines += '## 致命错误'
        $lines += ''
        $lines += '```'
        $lines += $script:FatalError
        $lines += '```'
        $lines += ''
    }
    $lines += '## 步骤'
    $lines += ''
    $lines += '| 步骤 | 状态 | 说明 |'
    $lines += '| --- | --- | --- |'
    foreach ($step in $script:Steps) {
        $detail = ([string]$step.Detail).Replace('|', '\|')
        $lines += ('| ' + $step.Name + ' | ' + $step.Status + ' | ' + $detail + ' |')
    }
    $lines += ''
    $lines += '## pytest'
    $lines += ''
    $lines += '| 套件 | 退出码 | 日志 |'
    $lines += '| --- | ---: | --- |'
    foreach ($key in @('crawler', 'backend', 'export')) {
        $code = '未运行'
        if ($script:PytestResults.ContainsKey($key)) { $code = $script:PytestResults[$key] }
        $logName = 'pytest_' + $key + '.txt'
        $lines += ('| ' + $key + ' | ' + $code + ' | ' + $script:LogRel + '\' + $logName + ' |')
    }
    $lines += ''
    $lines += '## 小量采集（关键词：星星人，每平台 1 页，详情最多 5 个）'
    $lines += ''
    if ($script:CrawlResults.Count -eq 0) {
        $lines += '> 未执行采集。'
    } else {
        $lines += '| 平台 | 状态 | 列表条数 | 登录 | risk_blocked | 详情状态 | 详情条数 | 日志 |'
        $lines += '| --- | --- | ---: | --- | --- | --- | ---: | --- |'
        foreach ($item in $script:CrawlResults) {
            $lines += ('| ' + $item.Platform + ' | ' + $item.Status + ' | ' + $item.Items + ' | ' + $item.LoginOk + ' | ' + $item.RiskBlocked + ' | ' + $item.DetailStatus + ' | ' + $item.DetailCount + ' | ' + $item.Log + ' |')
        }
        $lines += ''
        $lines += '### Edge 端口处理（只优雅关闭本次启动的实例）'
        $lines += ''
        $lines += '| 平台 | 端口 | 本次启动 | 已优雅退出 |'
        $lines += '| --- | ---: | --- | --- |'
        foreach ($item in $script:CloseResults) {
            $lines += ('| ' + $item.Platform + ' | ' + $item.Port + ' | ' + $item.Started + ' | ' + $item.Closed + ' |')
        }
        $lines += ''
        $lines += '> 1688 完整详情本轮停用（配置开关默认关），列表数据照常采集；命中关键词且缺店名的候选按配置上限补抓店名。'
        $lines += ''
        $lines += '> 脚本没有使用 taskkill / Stop-Process；只对本脚本自己启动的 Edge 端口，'
        $lines += '> 通过 crawler\.venv 的 `wu_tools\cdp_close.py` 发送 CDP `Browser.close`（不再用 PowerShell Add-Type）。'
    }
    $lines += ''
    $lines += '## trial_metrics'
    $lines += ''
    if ($script:TrialMetrics) {
        $lines += ('- 去重后总条数：' + $script:TrialMetrics.total_unique)
        $lines += ('- JSON：`' + $script:LogRel + '\trial_metrics.json`')
        $lines += ('- Markdown：`' + $script:LogRel + '\trial_metrics.md`')
        if ($script:TrialMetrics.platforms -and @($script:TrialMetrics.platforms.PSObject.Properties).Count -gt 0) {
            $lines += ''
            $lines += '| 平台 | 列表条数 | 进入详情 | 详情成功 | 评论数取到 | 店铺名非空率 | 候选店名非空率 | 评论非空率 | 模糊解析成功率 |'
            $lines += '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'
            foreach ($name in $script:TrialMetrics.platforms.PSObject.Properties.Name) {
                $p = $script:TrialMetrics.platforms.$name
                $lines += ('| ' + $name + ' | ' + $p.list_count + ' | ' + $p.detail_count + ' | ' + $p.detail_success + ' | ' + $p.comments_actual + ' | ' + (Format-Rate $p.shop_name_rate) + ' | ' + (Format-Rate $p.candidate_shop_name_rate) + ' | ' + (Format-Rate $p.comments_rate) + ' | ' + (Format-Rate $p.fuzzy_rate) + ' |')
            }
            $lines += ''
            $lines += ('- run_logs：' + $script:TrialMetrics.run_log_count + ' 行（每平台每次运行至少 1 行，含登录状态/条数/起止时间）')
        } else {
            $lines += ''
            $lines += '> 没有可统计的 JSONL 记录（采集可能全部失败）。'
        }
    } else {
        $lines += '> 未生成 trial_metrics。'
    }
    $lines += ''
    $lines += '## 星星人打分'
    $lines += ''
    if ($script:Ultraman) {
        $lines += ('- 临时库：`' + [string]$script:Ultraman.db + '`')
        $lines += ('- 报告：`' + [string]$script:Ultraman.report_md + '`')
        $lines += ('- 本轮采集日期：`' + [string]$script:Ultraman.run_date + '`')
        $lines += ''
        $lines += '| 口径 | 快照数 | 候选数 | need_vision | ip_hit |'
        $lines += '| --- | ---: | ---: | ---: | ---: |'
        $currentSummary = $script:Ultraman.current
        $allSummary = $script:Ultraman.all
        $lines += ('| 本轮采集（' + [string]$script:Ultraman.run_date + '） | ' + $currentSummary.total + ' | ' + $currentSummary.candidates + ' | ' + $currentSummary.need_vision + ' | ' + $currentSummary.ip_hit + ' |')
        $lines += ('| ' + [string]$script:Ultraman.history_label + ' | ' + $allSummary.total + ' | ' + $allSummary.candidates + ' | ' + $allSummary.need_vision + ' | ' + $allSummary.ip_hit + ' |')
        $lines += ''
        $lines += '| text_score 区间 | 本轮采集 | 含历史数据 |'
        $lines += '| --- | ---: | ---: |'
        foreach ($name in @('0', '1-19', '20-34', '35-59', '60+')) {
            $lines += ('| ' + $name + ' | ' + $currentSummary.bins.PSObject.Properties[$name].Value + ' | ' + $allSummary.bins.PSObject.Properties[$name].Value + ' |')
        }
    } else {
        $lines += '> 未生成 ultraman_score。'
    }
    $lines += ''
    $lines += '## 配置还原 / MD5'
    $lines += ''
    if ($script:RestoreResults.Count -eq 0) {
        $lines += '> 未进入配置修改阶段，无需还原。'
    } else {
        $lines += '| 文件 | 已还原 | MD5 before | MD5 after | 一致 |'
        $lines += '| --- | --- | --- | --- | --- |'
        foreach ($item in $script:RestoreResults) {
            $lines += ('| ' + $item.File + ' | ' + $item.Restored + ' | ' + $item.Md5Before + ' | ' + $item.Md5After + ' | ' + $item.Match + ' |')
        }
    }
    $lines += ''
    $lines += '## 错误 / 警告'
    $lines += ''
    if ($script:Errors.Count -eq 0) {
        $lines += '- 无'
    } else {
        foreach ($item in $script:Errors) {
            $lines += ('- **' + $item.Stage + '**：' + ([string]$item.Message).Replace('|', '\|'))
        }
    }
    $lines += ''
    $lines += '## 边界说明'
    $lines += ''
    $lines += ('- 只写 `' + $script:LogRel + '\`、`' + $script:OutRel + '\`、`' + $script:BackupRel + '\`；未写 `backend\data\leads.db`。')
    $lines += '- 1688 完整详情本轮停用；仅补抓缺店名候选的店名，不改评论数/店铺 ID。淘宝/1688 缺评论数只标记 `评论缺失`，绝不用京东评论补。'
    $lines += '- Edge 收尾只调用 crawler\.venv 的 `wu_tools\cdp_close.py`，未使用 Add-Type / taskkill / Stop-Process。'
    $lines += '- 未发送任何邮件（pipeline 使用 `--dry-run --no-alert`，临时配置还关闭了 export）。'
    $lines += '- 打分统计使用 dry-run 保留的临时 sqlite；包装器只阻止删除该临时目录，未改业务代码。'
    $lines += '- `crawler\out\2026-11-05` 缺失时会回退 `crawler\samples\2026-11-05`，已在日志标注。'
    $lines += '- 若 pytest/采集失败，脚本继续执行后续能做的步骤，并在本文件记录。'
    $lines += ''
    $lines += ('完整日志：`' + $script:LogRel + '\validate.log`')
    $lines += ''

    try {
        [System.IO.File]::WriteAllText($script:SummaryPath, ($lines -join "`r`n") + "`r`n", $script:Utf8NoBom)
        Write-Log ('SUMMARY 已写入: ' + $script:SummaryPath)
    } catch {
        Write-Log ('写 SUMMARY 失败: ' + $_.Exception.Message) 'ERROR'
    }

    $doneLines = @(
        'DONE',
        ('status=' + $status),
        ('finished_at=' + $finished.ToString('yyyy-MM-dd HH:mm:ss')),
        ('date=' + $script:Date),
        ('summary=' + $script:SummaryPath),
        ('log=' + $script:LogPath),
        ('errors=' + $script:Errors.Count),
        ('crawl_ok_platforms=' + $crawlOk),
        ('pytest_all_pass=' + $pytestOk),
        ('config_md5_match=' + $restoreOk)
    )
    try {
        [System.IO.File]::WriteAllText($script:DonePath, ($doneLines -join "`r`n") + "`r`n", $script:Utf8NoBom)
        Write-Log ('DONE 已写入: ' + $script:DonePath + ' status=' + $status)
    } catch {
        Write-Log ('写 DONE 失败: ' + $_.Exception.Message) 'ERROR'
    }
}

# ===========================================================================
# 主流程
# ===========================================================================
try {
    if ($script:Date -notmatch '^\d{4}-\d{2}-\d{2}$') { throw ('日期格式错误: ' + $script:Date) }
    if (-not (Test-Path -LiteralPath $script:Root)) { throw ('根目录不存在: ' + $script:Root) }
    Write-Log ('脚本: ' + $script:ScriptPath)
    Write-Log ('PowerShell: ' + $PSVersionTable.PSVersion.ToString())

    try {
        Backup-AndExtract
    } catch {
        $script:FatalError = '备份/解压失败: ' + $_.Exception.Message
        Add-Error 'backup/extract' $script:FatalError
        throw
    }

    $crawlerPy = Get-ModulePython 'crawler'
    $backendPy = Resolve-BackendPython -CrawlerPython $crawlerPy
    $script:BackendPython = $backendPy
    Write-Log ('crawler python: ' + $(if ($crawlerPy) { $crawlerPy } else { '缺失' }))
    Write-Log ('backend python: ' + $(if ($backendPy) { $backendPy } else { '缺失' }) + ' source=' + $script:BackendPythonSource)
    if ($script:BackendFallback) {
        Write-Log ('backend 回退说明: ' + $script:BackendNote) 'WARN'
    }

    # 第 8 条同步检查：backend 调 crawler 时必须优先用 crawler\.venv 的解释器；
    # 这里显式验证 crawler venv 能 import pydantic，并确认 backend 选择器文件存在。
    $crawlerPythonHelper = Join-Path $script:Root 'backend\pipeline\crawler_python.py'
    if (Test-Path -LiteralPath $crawlerPythonHelper) {
        Add-Step 'backend crawler python' 'OK' 'backend 调用 crawler 时优先 crawler\.venv\Scripts\python.exe'
    } else {
        Add-Error 'backend crawler python' ('缺少选择器: ' + (Show-Path $crawlerPythonHelper))
    }
    if ($crawlerPy) {
        $crawlerCheckLog = Join-Path $script:LogDir 'crawler_python_check.txt'
        $crawlerCheck = Invoke-External -Label 'crawler-python-check' -FilePath $crawlerPy -Arguments @('-c', 'import pydantic, yaml') -WorkingDirectory (Join-Path $script:Root 'crawler') -LogPath $crawlerCheckLog
        if ($crawlerCheck -eq 0) {
            Add-Step 'crawler python' 'OK' ((Show-Path $crawlerPy) + ' import pydantic/yaml 正常')
        } else {
            Add-Error 'crawler python' ('crawler venv 缺依赖 exit=' + $crawlerCheck + '，详见 ' + (Show-Path $crawlerCheckLog))
        }
    }

    try {
        if ($crawlerPy) {
            Install-Requirements -Label 'crawler' -Python $crawlerPy -Requirements (Join-Path $script:Root 'crawler\requirements.txt') -LogName 'pip_crawler.txt' | Out-Null
        } else {
            Add-Error 'pip:crawler' 'crawler\.venv\Scripts\python.exe 不存在'
        }
        if ($backendPy) {
            $backendPipOk = Install-Requirements -Label 'backend' -Python $backendPy -Requirements (Join-Path $script:Root 'backend\requirements.txt') -LogName 'pip_backend.txt' -Soft
            $backendPipOk = [bool]$backendPipOk
            if ((-not $backendPipOk) -and $crawlerPy -and ($backendPy -ne $crawlerPy)) {
                $reason = 'backend\.venv 依赖安装失败，已回退 crawler\.venv 跑 backend pytest 与打分（SUMMARY 已标注）'
                $backendPy = Enable-BackendFallback -Reason $reason -CrawlerPython $crawlerPy
                $script:BackendPython = $backendPy
                Install-Requirements -Label 'backend(crawler fallback)' -Python $crawlerPy -Requirements (Join-Path $script:Root 'backend\requirements.txt') -LogName 'pip_backend_fallback.txt' | Out-Null
            }
            $exportReq = Join-Path $script:Root 'export\requirements-dev.txt'
            if (Test-Path -LiteralPath $exportReq) {
                Install-Requirements -Label 'export(backend venv)' -Python $backendPy -Requirements $exportReq -LogName 'pip_export.txt' -Soft | Out-Null
            }
        } else {
            Add-Error 'pip:backend' 'backend\.venv\Scripts\python.exe 不存在，且 crawler python 不可用'
        }
    } catch {
        Add-Error 'pip' ('依赖安装阶段异常: ' + $_.Exception.Message)
    }

    try {
        Run-Pytest -Label 'crawler' -Python $crawlerPy -WorkingDirectory (Join-Path $script:Root 'crawler') -LogName 'pytest_crawler.txt' | Out-Null
        Run-Pytest -Label 'backend' -Python $backendPy -WorkingDirectory (Join-Path $script:Root 'backend') -LogName 'pytest_backend.txt' | Out-Null
        $exportPy = $backendPy
        if (-not $exportPy) { $exportPy = $crawlerPy }
        Run-Pytest -Label 'export' -Python $exportPy -WorkingDirectory (Join-Path $script:Root 'export') -LogName 'pytest_export.txt' | Out-Null
    } catch {
        Add-Error 'pytest' ('pytest 阶段异常: ' + $_.Exception.Message)
    }

    try {
        Invoke-CrawlPhase
    } catch {
        Add-Error 'crawl' ('采集阶段异常: ' + $_.Exception.Message)
    }

    try {
        Invoke-RunEntrySmoke
    } catch {
        Add-Error 'run_entry' ('run-entry 阶段异常: ' + $_.Exception.Message)
    }

    try {
        Invoke-TrialMetrics
    } catch {
        Add-Error 'trial_metrics' ('trial_metrics 阶段异常: ' + $_.Exception.Message)
    }

    try {
        Invoke-UltramanScoring
    } catch {
        Add-Error 'ultraman' ('打分阶段异常: ' + $_.Exception.Message)
    }
} catch {
    if (-not $script:FatalError) {
        $script:FatalError = $_.Exception.Message
        Add-Error 'fatal' $script:FatalError
    }
} finally {
    try {
        Write-SummaryAndDone
    } catch {
        Write-Log ('写 SUMMARY/DONE 出现异常: ' + $_.Exception.Message) 'ERROR'
        try {
            $fallbackSummary = @(
                '# WU 一次性验证摘要（2026-11-06）',
                '',
                '- 结论：**FAIL**',
                ('- 生成摘要时异常：' + $_.Exception.Message),
                ('- 完整日志：`' + $script:LogPath + '`')
            ) -join "`r`n"
            [System.IO.File]::WriteAllText($script:SummaryPath, $fallbackSummary + "`r`n", $script:Utf8NoBom)
        } catch { }
        try {
            $fallback = 'DONE' + "`r`n" + 'status=FAIL' + "`r`n" + 'error=' + $_.Exception.Message + "`r`n"
            [System.IO.File]::WriteAllText($script:DonePath, $fallback, $script:Utf8NoBom)
        } catch { }
    }
}

if ($script:FatalError) { exit 1 }
if ($script:Errors.Count -gt 0) { exit 2 }
exit 0
