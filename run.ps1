$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
python -m http.server 8878

