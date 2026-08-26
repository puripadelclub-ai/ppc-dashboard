# push-competitor-scraper.ps1
# Deploy Option B: Ayo.co.id competitor occupancy scraper
# Run from PowerShell in the ppc-dashboard-code folder

Set-Location $PSScriptRoot

Write-Host "`n=== Git Status ===" -ForegroundColor Cyan
git status

Write-Host "`n=== Staging files ===" -ForegroundColor Cyan
git add lib/competitor_client.py
git add api/process.py
git add vercel.json

Write-Host "`n=== Files staged ===" -ForegroundColor Cyan
git status --short

Write-Host "`n=== Committing ===" -ForegroundColor Cyan
$msg = @'
feat: dual-fetch schedule + smart merge for competitor occupancy

competitor_client.py:
- Expand registry to 39 venues (full Drive benchmark coverage)
- accumulate_competitors(): is_evening_run param for smart merge
  Morning run: replace today rows (full pre-booked snapshot)
  Evening run: preserve morning_occ + overall_occ, update afternoon+evening
- Dynamic courts count from API fields when not hardcoded
- max_workers=10 for 39-venue parallel fetch

api/process.py:
- fetch-competitors: auto-detect morning/evening from WIB hour
- Accepts run_type query param: morning | evening | auto
- Passes is_evening_run to accumulate_competitors()

vercel.json:
- Morning cron: 0 22 * * * = 05:00 WIB (full snapshot)
- Evening cron: 0 10 * * * = 17:00 WIB (smart merge)
'@
git commit -m $msg

Write-Host "`n=== Pushing to origin/main ===" -ForegroundColor Cyan
git push origin main

Write-Host "`n=== Done! ===" -ForegroundColor Green
Write-Host "Next: trigger /api/fetch-competitors to re-seed today's data with corrected format" -ForegroundColor Yellow
Write-Host "URL: https://ppc-dashboard-zeta.vercel.app/api/fetch-competitors" -ForegroundColor Yellow
