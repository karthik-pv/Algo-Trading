param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$CommitMessage
)

Write-Host "Adding changes..." -ForegroundColor Cyan
git add .

if ($LASTEXITCODE -ne 0) {
    Write-Host "git add failed." -ForegroundColor Red
    exit 1
}

Write-Host "Committing: $CommitMessage" -ForegroundColor Cyan
git commit -m "$CommitMessage"

if ($LASTEXITCODE -ne 0) {
    Write-Host "git commit failed." -ForegroundColor Red
    exit 1
}

Write-Host "Pushing..." -ForegroundColor Cyan
git push

if ($LASTEXITCODE -ne 0) {
    Write-Host "git push failed." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Git add, commit and push completed successfully." -ForegroundColor Green