$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
Set-Location -LiteralPath $project
$env:TEMP = Join-Path $project '.local\tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $project '.local\pip-cache'
$env:npm_config_cache = Join-Path $project '.local\npm-cache'
$env:ELECTRON_CACHE = Join-Path $project '.local\electron-cache'
$env:ELECTRON_BUILDER_CACHE = Join-Path $project '.local\electron-builder-cache'
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
& .\desktop\node_modules\.bin\esbuild.cmd .\desktop\ui-entry.js --bundle --minify --format=iife --outfile=connector/static/widgets.js --legal-comments=linked
if ($LASTEXITCODE) { throw 'UI bundle failed' }
$staticData = (Join-Path $project 'connector\static') + ';connector/static'
$resourceData = (Join-Path $project 'connector\resources') + ';connector/resources'
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --name AIGentServer --onedir --distpath .local/build/backend --workpath .local/build/pyinstaller --specpath .local/build --add-data $staticData --add-data $resourceData --collect-submodules claude_agent_sdk --collect-submodules uvicorn --collect-submodules connector --collect-submodules trimesh run.py
if ($LASTEXITCODE) { throw 'Backend packaging failed' }
Set-Location -LiteralPath (Join-Path $project 'desktop')
& npm.cmd run pack
if ($LASTEXITCODE) { throw 'Electron packaging failed' }
