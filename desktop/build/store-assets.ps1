param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$icon = [System.Drawing.Image]::FromFile($Source)
try {
    # Preserve the existing artwork and aspect ratio, including on the wide tile.
    $assets = @{
        'StoreLogo.png' = @(50, 50)
        'Square44x44Logo.png' = @(44, 44)
        'Square150x150Logo.png' = @(150, 150)
        'Wide310x150Logo.png' = @(310, 150)
    }
    foreach ($name in $assets.Keys) {
        $width, $height = $assets[$name]
        $bitmap = [System.Drawing.Bitmap]::new($width, $height)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.Clear([System.Drawing.Color]::Transparent)
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $scale = [Math]::Min($width / $icon.Width, $height / $icon.Height)
            $w = [int][Math]::Round($icon.Width * $scale)
            $h = [int][Math]::Round($icon.Height * $scale)
            $graphics.DrawImage($icon, [int](($width - $w) / 2), [int](($height - $h) / 2), $w, $h)
            $bitmap.Save((Join-Path $Destination $name), [System.Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }
} finally {
    $icon.Dispose()
}
