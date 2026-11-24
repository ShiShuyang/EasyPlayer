param(
    [string]$NsisCompiler,
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$Version = '1.0.0'
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
if (-not $NsisCompiler) {
    $localCompiler = Join-Path $project '.setup-tools\msys\mingw64\bin\makensis.exe'
    if (Test-Path -LiteralPath $localCompiler -PathType Leaf) {
        $NsisCompiler = $localCompiler
    } else {
        $compiler = Get-Command makensis.exe -ErrorAction SilentlyContinue
        $NsisCompiler = if ($compiler) { $compiler.Source } else {
            Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe'
        }
    }
}
if (-not (Test-Path -LiteralPath $NsisCompiler -PathType Leaf)) {
    throw "NSIS compiler not found at '$NsisCompiler'. Pass the full path to makensis.exe with -NsisCompiler."
}

$productName = 'EasyPlayer'
$runtimeDirectory = "${productName}Runtime"
$publisher = '施舒阳'
$ribbonIcons = Join-Path $project 'icon-designs\ribbon'
$playerIcon = Join-Path $ribbonIcons 'player.ico'
$venv = Join-Path $project '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The workspace .venv was not found.'
}
& $python -c "import platform, struct, PyInstaller; assert struct.calcsize('P') == 8 and platform.machine().upper() == 'AMD64', 'Use Windows x64 Python.'"
if ($LASTEXITCODE -ne 0) { throw 'Install the build dependencies into the Windows x64 .venv first.' }

$csharp = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csharp -PathType Leaf)) {
    throw 'The Windows .NET Framework x64 C# compiler was not found.'
}

$build = Join-Path $project '.installer-build'
$release = Join-Path $project 'release'
New-Item -ItemType Directory -Path $build, $release -Force | Out-Null
$versionResource = Join-Path $build 'version.txt'
$numericVersion = (($Version.Split('.') | ForEach-Object { [UInt16]$_ }) -join ', ') + ', 0'
@"
VSVersionInfo(
    ffi=FixedFileInfo(filevers=($numericVersion), prodvers=($numericVersion),
                      mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('080404b0', [
        StringStruct('CompanyName', '$publisher'),
        StringStruct('FileDescription', '简放'),
        StringStruct('FileVersion', '$Version'),
        StringStruct('InternalName', '$productName'),
        StringStruct('OriginalFilename', '${productName}.exe'),
        StringStruct('ProductName', '$productName'),
        StringStruct('ProductVersion', '$Version'),
    ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])],
)
"@ | Set-Content -LiteralPath $versionResource -Encoding utf8

$previousCache = $env:PYINSTALLER_CONFIG_DIR
$previousPath = $env:PATH
$env:PYINSTALLER_CONFIG_DIR = Join-Path $build 'cache'
# Keep other installed toolchains' DLLs out of PyInstaller's dependency search.
$env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
Push-Location -LiteralPath $project
try {
    & $python -m PyInstaller --clean --noconfirm --onedir --windowed --noupx --name $productName `
        --contents-directory $runtimeDirectory --icon $playerIcon --version-file $versionResource `
        --additional-hooks-dir (Join-Path $PSScriptRoot 'hooks') `
        --distpath (Join-Path $build 'bundle') --workpath (Join-Path $build 'work') --specpath $build `
        --add-data "${playerIcon}:icon-designs/ribbon" `
        --add-data "$project\player.qss:." --add-data "$project\flat_video.qml:." (Join-Path $project 'player.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the player.' }
} finally {
    Pop-Location
    $env:PYINSTALLER_CONFIG_DIR = $previousCache
    $env:PATH = $previousPath
}

$payload = Join-Path (Join-Path $build 'bundle') $productName
$translations = Join-Path $payload "$runtimeDirectory\PySide6\translations"
Get-ChildItem -LiteralPath $translations -File -Filter '*.qm' |
    Where-Object { $_.BaseName -notmatch '_(en|zh)(_[A-Za-z]+)*$' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName }
$fileIcons = Join-Path $payload 'icons'
New-Item -ItemType Directory -Path $fileIcons -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $ribbonIcons 'audio.ico'), (Join-Path $ribbonIcons 'video.ico') -Destination $fileIcons
& $csharp /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll `
    "/out:$(Join-Path $payload "${productName}Shell.exe")" (Join-Path $PSScriptRoot 'ShellPlaylist.cs')
if ($LASTEXITCODE -ne 0) { throw 'The C# compiler could not build the Explorer playlist command.' }

$licenseDirectory = Join-Path (Join-Path $payload $runtimeDirectory) 'Licenses'
New-Item -ItemType Directory -Path $licenseDirectory -Force | Out-Null
$sitePackages = Join-Path $venv 'Lib\site-packages'
foreach ($package in @('pyside6_addons', 'pyside6_essentials', 'shiboken6', 'pyinstaller')) {
    $metadata = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "$package-*.dist-info" | Select-Object -First 1
    if ($metadata) {
        $licenses = Join-Path $metadata.FullName 'licenses'
        if (Test-Path -LiteralPath $licenses -PathType Container) {
            $destination = Join-Path $licenseDirectory $package
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
            Get-ChildItem -LiteralPath $licenses | Copy-Item -Destination $destination -Recurse -Force
        }
    }
}
$basePython = & $python -c 'import sys; print(sys.base_prefix)'
$pythonLicense = Join-Path $basePython 'LICENSE.txt'
if (Test-Path -LiteralPath $pythonLicense -PathType Leaf) {
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseDirectory 'Python.txt')
}

$setupName = "$productName-$Version-Setup-x64.exe"
$staging = Join-Path $build $setupName
$output = Join-Path $release $setupName
& $NsisCompiler /V2 /INPUTCHARSET UTF8 "/DAPP_VERSION=$Version" "/DPAYLOAD_DIR=$payload" `
    "/DAPP_PRODUCT=$productName" "/DAPP_PUBLISHER=$publisher" `
    "/DAPP_ICON=$playerIcon" "/DOUTPUT_EXE=$staging" (Join-Path $PSScriptRoot 'installer.nsi')
if ($LASTEXITCODE -ne 0) { throw 'NSIS could not build the installer.' }
Move-Item -LiteralPath $staging -Destination $output -Force
Get-Item -LiteralPath $output
