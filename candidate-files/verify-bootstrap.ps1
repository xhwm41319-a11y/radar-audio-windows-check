param([Parameter(Mandatory = $true)][string]$BundleRoot)

$ErrorActionPreference = 'Stop'
$BundleRoot = [IO.Path]::GetFullPath($BundleRoot).TrimEnd('\')
try {
    $manifest = Join-Path $BundleRoot 'payload-sha256.json'
    $rows = @(Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json)
    $prefix = '.bootstrap/python/'
    $seen = @{}
    foreach ($row in $rows) {
        $name = [string]$row.path
        if (-not $name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($name -match '[\\:]' -or $name -match '(^|/)\.\.?(/|$)' -or $name.StartsWith('/')) {
            throw "Invalid bootstrap path: $name"
        }
        $key = $name.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "Duplicate bootstrap path: $name" }
        $seen[$key] = $true
        $file = Join-Path $BundleRoot ($name.Replace('/', '\'))
        $item = Get-Item -LiteralPath $file -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.PSIsContainer) {
            throw "Bootstrap file is not regular: $name"
        }
        if ($item.Length -ne [long]$row.bytes -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$row.sha256) {
            throw "Bootstrap file hash mismatch: $name"
        }
    }
    if (-not $seen.ContainsKey('.bootstrap/python/python.exe')) {
        throw 'python.exe is absent from bootstrap manifest'
    }
    $bootstrap = Join-Path $BundleRoot '.bootstrap\python'
    foreach ($item in Get-ChildItem -LiteralPath $bootstrap -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Bootstrap contains a link: $($item.FullName)"
        }
        if ($item.PSIsContainer) { continue }
        $relative = $item.FullName.Substring($BundleRoot.TrimEnd('\').Length + 1).Replace('\', '/').ToLowerInvariant()
        if (-not $seen.ContainsKey($relative)) { throw "Unlisted bootstrap file: $relative" }
    }
    Write-Host "Bootstrap SHA256 verified: $($seen.Count) files"
    exit 0
} catch {
    Write-Error "Bootstrap verification failed: $($_.Exception.Message)"
    exit 2
}
