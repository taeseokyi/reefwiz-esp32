# kfont 래스터라이저 — Windows .NET(System.Drawing)으로 charset.txt 의 글자를
# 16x16(한글·기호) / 8x16(ASCII) 비트맵으로 굽는다. 출력: glyphs.jsonl (한 줄 = 한 글자)
# 한글 = 맑은 고딕 / ASCII = Consolas. 이후 pack_kfont.py 가 kfont.bin 으로 패킹.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$charset = [IO.File]::ReadAllText("$here\charset.txt", [Text.Encoding]::UTF8)
$ascii = -join (32..126 | ForEach-Object { [char]$_ })
$all = $ascii + $charset

$fontKo = New-Object System.Drawing.Font("Malgun Gothic", 12, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
$fontEn = New-Object System.Drawing.Font("Consolas", 13, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
$sf = [System.Drawing.StringFormat]::GenericTypographic

$out = New-Object IO.StreamWriter("$here\glyphs.jsonl", $false, (New-Object Text.UTF8Encoding($false)))
$count = 0
foreach ($ch in $all.ToCharArray()) {
    $code = [int]$ch
    $isAscii = $code -lt 127
    $w = if ($isAscii) { 8 } else { 16 }
    $font = if ($isAscii) { $fontEn } else { $fontKo }
    $bmp = New-Object System.Drawing.Bitmap(16, 16)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::SingleBitPerPixelGridFit
    $g.Clear([System.Drawing.Color]::Black)
    # 셀 중앙 정렬: 글리프 실측 폭 기준 가로 오프셋, 세로는 폰트별 고정 보정
    $sz = $g.MeasureString($ch, $font, [System.Drawing.PointF]::Empty, $sf)
    $ox = [Math]::Max(0, [Math]::Floor(($w - $sz.Width) / 2))
    $oy = if ($isAscii) { 1 } else { 0 }
    $g.DrawString($ch, $font, [System.Drawing.Brushes]::White, $ox, $oy, $sf)
    $g.Dispose()
    $rows = @()
    for ($y = 0; $y -lt 16; $y++) {
        $bits = 0
        for ($x = 0; $x -lt $w; $x++) {
            if ($bmp.GetPixel($x, $y).R -gt 100) { $bits = $bits -bor (0x8000 -shr $x) }
        }
        $rows += $bits
    }
    $bmp.Dispose()
    $out.WriteLine(('{{"c":{0},"w":{1},"r":[{2}]}}' -f $code, $w, ($rows -join ",")))
    $count++
}
$out.Close()
Write-Output "glyphs.jsonl: $count chars"
