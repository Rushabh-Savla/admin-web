# PowerShell Launcher - Opens Central Server Web Platform
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  NAS Data Annotation Central Web Platform" -ForegroundColor Green
Write-Host "  Central Server URL: http://192.168.1.103:8080/dashboard" -ForegroundColor Yellow
Write-Host "  Opening in default browser..." -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan

Start-Process "http://192.168.1.103:8080/dashboard"
