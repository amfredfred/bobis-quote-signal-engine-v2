# powershell -ExecutionPolicy Bypass -File run_backtests.ps1
#
# Full validation backtest — all 5 pairs, 3-year window.
# Uses config.yaml settings (min_rr=8, max_rr=3 per symbol via rrr block).

$FROM   = "2026-01-01"
$TO     = "2026-06-13"
$OUTDIR = "results\exness-6m-validation"
$null   = New-Item -ItemType Directory -Force $OUTDIR

$symbols = @("XAUUSD", "US100", "EURUSD", "GBPUSD", "USDJPY")
$cwd     = (Get-Location).Path

$jobs = $symbols | ForEach-Object {
    $sym = $_
    Start-Job -ScriptBlock {
        param($s, $dir, $from, $to, $out)
        $env:PYTHONUTF8 = "1"
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $OutputEncoding            = [System.Text.Encoding]::UTF8
        Set-Location $dir
        & "$dir\venv\Scripts\python.exe" -m src.app.backtesting.backtest `
            --symbol     $s `
            --risk-percent 0.2 `
            --start-balance 5000 `
            --max-trailing-dd-pct 3 `
            --risk-sweep `
            --from-date  $from `
            --to-date    $to `
            --output     "$out\$s.csv" `
            2>&1 | ForEach-Object { "[$s] $_" }
    } -ArgumentList $sym, $cwd, $FROM, $TO, $OUTDIR
}

Write-Host "Running $($symbols.Count) backtests in parallel ($FROM → $TO)..."

while ($jobs | Where-Object { $_.State -eq 'Running' }) {
    $jobs | Receive-Job
    Start-Sleep -Milliseconds 300
}

$jobs | Receive-Job
$jobs | Remove-Job

Write-Host "`nDone. Results in: $OUTDIR"

# Quick PF summary
Write-Host "`nPer-pair summary:"
foreach ($sym in $symbols) {
    $csv = "$OUTDIR\$sym.csv"
    if (-not (Test-Path $csv)) { Write-Host "  $sym - no output"; continue }
    $rows = Import-Csv $csv
    $wins  = ($rows | Where-Object { $_.outcome -in @("WIN_FULL","WIN_PARTIAL","WIN") } | Measure-Object -Property realized_rr -Sum).Sum
    $loss  = ($rows | Where-Object { $_.outcome -in @("LOSS","SL_HIT") } | Measure-Object -Property realized_rr -Sum).Sum
    $lossAbs = [Math]::Abs($loss)
    $pf    = if ($lossAbs -gt 0) { [Math]::Round($wins / $lossAbs, 2) } else { "inf" }
    Write-Host "  $sym  trades=$($rows.Count)  PF=$pf  gross_R=$([Math]::Round($wins + $loss, 1))"
}
