# powershell -ExecutionPolicy Bypass -File run_backtests.ps1

$null = New-Item -ItemType Directory -Force "results\2019-2026-FREQUENCY"

$symbols = @("XAUUSD", "US30", "US500", "US100", "JP225", "EURUSD")
$cwd = (Get-Location).Path

$jobs = $symbols | ForEach-Object {
    $sym = $_
    Start-Job -ScriptBlock {
        param($s, $dir)
        Set-Location $dir                          # ← restore working directory
        py -m src.app.backtesting.backtest --symbol $s --from-date 2019-01-01 --output "results/2019-2026-FREQUENCY/$s.csv" 2>&1 | ForEach-Object { "[$s] $_" }
    } -ArgumentList $sym, $cwd
}

while ($jobs | Where-Object { $_.State -eq 'Running' }) {
    $jobs | Receive-Job
    Start-Sleep -Milliseconds 200
}

$jobs | Receive-Job
$jobs | Remove-Job

Write-Host "`n✅ All 6 backtests complete."