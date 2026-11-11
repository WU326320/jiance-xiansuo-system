# 工作台 v1 一键部署 + 自检
# 约束：只操作项目目录内的文件；不删除项目目录以外的任何文件。
param(
    [switch]$SkipTests,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Assert-InsideProject([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作项目目录以外的路径：$full"
    }
    return $full
}

Write-Host "== 工作台 v1 部署 ==" -ForegroundColor Cyan
Write-Host "项目目录：$ProjectRoot"

$backendVenv = Join-Path $ProjectRoot "backend\.venv"
$crawlerVenv = Join-Path $ProjectRoot "crawler\.venv"
$python = Join-Path $backendVenv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "backend\.venv 不存在，尝试创建……"
    $created = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -3.13 -m venv $backendVenv
            $created = $true
        } catch { $created = $false }
    }
    if (-not $created) {
        Write-Host "无法创建 backend\.venv，回退 crawler\.venv" -ForegroundColor Yellow
        $python = Join-Path $crawlerVenv "Scripts\python.exe"
    } else {
        $python = Join-Path $backendVenv "Scripts\python.exe"
    }
}
if (-not (Test-Path $python)) {
    $python = "python"
}
Write-Host "Python：$python"

if (-not $SkipInstall) {
    Write-Host "安装 backend 依赖……"
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $ProjectRoot "backend\requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "backend 依赖安装失败" }
    $wbReq = Join-Path $ProjectRoot "workbench\requirements.txt"
    & $python -m pip install -r $wbReq
    if ($LASTEXITCODE -ne 0) { throw "workbench 依赖安装失败" }
}

# 数据目录（只在项目内）
$dataDir = Join-Path $ProjectRoot "workbench\data"
$null = Assert-InsideProject $dataDir
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "logs") | Out-Null

Write-Host "初始化工作台数据库……"
& $python -m workbench init-db
if ($LASTEXITCODE -ne 0) { throw "工作台数据库初始化失败" }

Write-Host "运行自检……"
& $python -m workbench selfcheck
if ($LASTEXITCODE -ne 0) { throw "工作台自检未通过" }

if (-not $SkipTests) {
    Write-Host "运行工作台 pytest……"
    & $python -m pytest (Join-Path $ProjectRoot "workbench\tests") -q
    if ($LASTEXITCODE -ne 0) { throw "工作台测试未通过" }
}

Write-Host ""
Write-Host "部署与自检完成。" -ForegroundColor Green
Write-Host "首次启动：双击 启动工作台.bat，然后打开 http://127.0.0.1:8765/setup 设置主管密码。"
Write-Host "如未初始化主管，也可运行：$python -m workbench set-supervisor"
