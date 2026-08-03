param(
    [Parameter(Mandatory = $true)]
    [string]$ImagePath
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

function Await-WinRt {
    param(
        [Parameter(Mandatory = $true)]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )

    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq "AsTask" -and
            $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1
        } |
        Select-Object -First 1

    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$resolvedPath = (Resolve-Path -LiteralPath $ImagePath).Path
$file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedPath)) ([Windows.Storage.StorageFile])
$stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()

if ($null -eq $engine) {
    throw "Windows OCR language pack is unavailable. Install an OCR language in Windows Settings."
}

$result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$items = @()

foreach ($line in $result.Lines) {
    $left = [double]::PositiveInfinity
    $top = [double]::PositiveInfinity
    $right = 0.0
    $bottom = 0.0

    foreach ($word in $line.Words) {
        $rect = $word.BoundingRect
        $left = [Math]::Min($left, $rect.X)
        $top = [Math]::Min($top, $rect.Y)
        $right = [Math]::Max($right, $rect.X + $rect.Width)
        $bottom = [Math]::Max($bottom, $rect.Y + $rect.Height)
    }

    if ([double]::IsPositiveInfinity($left)) {
        $left = 0.0
        $top = 0.0
    }

    $items += @{
        text = $line.Text
        confidence = $null
        box = @{
            x = if ($decoder.PixelWidth) { $left / $decoder.PixelWidth } else { 0.0 }
            y = if ($decoder.PixelHeight) { $top / $decoder.PixelHeight } else { 0.0 }
            width = if ($decoder.PixelWidth) { ($right - $left) / $decoder.PixelWidth } else { 0.0 }
            height = if ($decoder.PixelHeight) { ($bottom - $top) / $decoder.PixelHeight } else { 0.0 }
        }
    }
}

@{
    backend = "windows-ocr"
    width = [int]$decoder.PixelWidth
    height = [int]$decoder.PixelHeight
    items = $items
} | ConvertTo-Json -Depth 6 -Compress
