param(
    [ValidateSet('check','openspec','promptfoo','pytest','lint-imports','gitleaks','python')]
    [string]$Tool = 'check',
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ToolArgs
)
. (Join-Path $PSScriptRoot 'devtools-env.ps1')
Set-Location -LiteralPath $devProjectRoot
$devPython = Join-Path $devProjectRoot 'tools\python\Scripts\python.exe'
$devNode = Join-Path $devNodeRoot 'node.exe'
switch ($Tool) {
    'openspec' {
        & $devNode (Join-Path $devProjectRoot 'tools\npm\node_modules\@fission-ai\openspec\bin\openspec.js') @ToolArgs
    }
    'promptfoo' {
        & $devNode (Join-Path $devProjectRoot 'tools\npm\node_modules\promptfoo\dist\src\entrypoint.js') @ToolArgs
    }
    'pytest' {
        & $devPython -m pytest -o cache_dir=tools/state/pytest-cache @ToolArgs
    }
    'lint-imports' {
        & (Join-Path $devProjectRoot 'tools\python\Scripts\lint-imports.exe') --config config\import-linter.ini --cache-dir tools\state\import-linter-cache @ToolArgs
    }
    'gitleaks' {
        & (Join-Path $devProjectRoot 'tools\gitleaks\gitleaks.exe') @ToolArgs --redact=100
    }
    'python' {
        & $devPython @ToolArgs
    }
    'check' {
        & (Join-Path $devProjectRoot 'tools\python\Scripts\ruff.exe') check src scripts tests run.py tools\smoke\test_devtools.py
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $devPython -m pytest -q tests tools\smoke\test_devtools.py -o cache_dir=tools/state/pytest-cache --basetemp=.tmp/pytest-devtools
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & (Join-Path $devProjectRoot 'tools\python\Scripts\lint-imports.exe') --config config\import-linter.ini --cache-dir tools\state\import-linter-cache --no-logo
    }
}
exit $LASTEXITCODE
