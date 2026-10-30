param(
    [switch]$Asr,
    [switch]$Browser,
    [switch]$PowerPoint,
    [switch]$SkipYoutube,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Command failed with exit code ${code}: $FilePath $($Arguments -join ' ')"
    }
}

try {
    if (-not (Test-Path ".venv")) {
        Invoke-Checked $Python "-m" "venv" ".venv"
    }
    $py = Join-Path $root ".venv\Scripts\python.exe"
    Invoke-Checked $py "-m" "pip" "install" "--upgrade" "pip"
    Invoke-Checked $py "-m" "pip" "install" "-r" "requirements.txt"
    if (-not $SkipYoutube) {
        Invoke-Checked $py "-m" "pip" "install" "-r" "requirements-youtube.txt"
    }
    if ($Asr) {
        Invoke-Checked $py "-m" "pip" "install" "-r" "requirements-asr.txt"
    }
    if ($PowerPoint) {
        Invoke-Checked $py "-m" "pip" "install" "pywin32>=306"
    }
    if ($Browser) {
        Invoke-Checked $py "-m" "playwright" "install" "chromium"
    }
    Write-Host ""
    Write-Host "Core setup complete."
    Write-Host "Interpreter: $py"
    Write-Host "CLI: $py scripts/lecture.py --help"
    Write-Host ""
    Write-Host "Local ASR model (not downloaded automatically):"
    Write-Host "  Place model.bin, config.json, tokenizer.json, and vocabulary.txt"
    Write-Host "  in .models/whisper-small/ or pass --model-dir."
    Write-Host ""
    Write-Host "PDF rendering: Playwright uses installed Edge/Chrome when available."
    Write-Host "YouTube: yt-dlp[default] includes the official EJS component."
    Write-Host "  Stream extraction also needs Deno (preferred) or Node.js available on PATH."
    Write-Host "  This setup does not install or download a JavaScript runtime."
    Write-Host "Run: $py scripts/lecture.py capabilities"
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}
