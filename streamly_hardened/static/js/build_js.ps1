$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $here "src"
$outPath = Join-Path $here "app.js"

$open = [System.IO.File]::ReadAllText((Join-Path $src "_wrap_open.txt"))
$close = [System.IO.File]::ReadAllText((Join-Path $src "_wrap_close.txt"))

$frags = Get-ChildItem $src -Filter *.js | Sort-Object Name
$body = ""
foreach ($f in $frags) {
    $body += [System.IO.File]::ReadAllText($f.FullName)
}

$out = $open + "`n" + $body + $close
if (!$out.EndsWith("`n")) {
    $out += "`n"
}

[System.IO.File]::WriteAllText($outPath, $out)
Write-Host "app.js rebuilt from $($frags.Count) fragments"
