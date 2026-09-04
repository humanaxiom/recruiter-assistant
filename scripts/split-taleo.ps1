<#
.SYNOPSIS
  Split ONE combined Taleo applicant-export PDF into per-applicant PDFs.

.DESCRIPTION
  The splitter itself lives at core/scripts/split_taleo_pdf.py so it is inside
  the worker image's bind mount (./core -> /app) and inside the lint/type
  gates. THIS file is only the plumbing: it mounts your input file and output
  directory into a throwaway worker container so you never have to think about
  container paths.

  There is no usable Python on this host and PyMuPDF only exists in the worker
  image, so running the splitter directly here will not work.

  The export is real candidate PII. Keep both paths outside the repo, or under
  the gitignored fixtures\ — nothing this writes should ever be committed.

.PARAMETER InputPdf
  The combined export PDF.

.PARAMETER Output
  Directory to write the per-applicant PDFs into. Created if missing.

.PARAMETER Rest
  Passed straight through to the splitter: -Zip, --heuristic,
  --ranges "1-2;3-5", --model <name>, --dry-run.

.EXAMPLE
  scripts\split-taleo.ps1 C:\exports\req7124.pdf -Output C:\exports\7124-split --zip
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)][string]$InputPdf,
    [Parameter(Mandatory)][string]$Output,
    [Parameter(ValueFromRemainingArguments)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InputPdf -PathType Leaf)) {
    throw "no such file: $InputPdf"
}
if (-not (Test-Path -LiteralPath $Output)) {
    New-Item -ItemType Directory -Path $Output -Force | Out-Null
}

# Absolute paths — a bind mount cannot take a relative one.
$inFull  = (Resolve-Path -LiteralPath $InputPdf).Path
$inDir   = Split-Path -Parent $inFull
$inFile  = Split-Path -Leaf $inFull
$outDir  = (Resolve-Path -LiteralPath $Output).Path

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    # `run --rm`, not `exec`: this needs no running stack, and it must not
    # leave a container holding a mount of a directory full of candidate PII.
    $dockerArgs = @(
        'compose', 'run', '--rm', '--no-deps',
        '-v', "${inDir}:/in:ro",
        '-v', "${outDir}:/out",
        'worker', 'python', 'scripts/split_taleo_pdf.py',
        "/in/$inFile", '--output', '/out'
    ) + $Rest
    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) { throw "splitter exited $LASTEXITCODE" }
}
finally { Pop-Location }
