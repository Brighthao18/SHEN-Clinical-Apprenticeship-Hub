param([ValidateSet('demo','demo-stop','doctor','setup','init','start','stop','test-api')][string]$Action = 'doctor')
$ErrorActionPreference = 'Stop'
python (Join-Path $PSScriptRoot 'shen.py') $Action
exit $LASTEXITCODE
