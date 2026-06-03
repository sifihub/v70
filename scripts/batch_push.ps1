$files = git ls-files --others --exclude-standard
$batchSize = 500
$totalFiles = $files.Count
Write-Host "Total files to add: $totalFiles"

$batchIndex = 1
for ($i = 0; $i -lt $totalFiles; $i += $batchSize) {
    $end = [Math]::Min($i + $batchSize - 1, $totalFiles - 1)
    $batch = $files[$i..$end]
    
    Write-Host "Staging batch $batchIndex ($($batch.Count) files)..."
    foreach ($file in $batch) {
        if ($file) {
            git add $file
        }
    }
    
    $commitMsg = "Forced mirror chromium101 part $batchIndex"
    Write-Host "Committing batch $batchIndex..."
    git commit -m $commitMsg
    
    Write-Host "Pushing batch $batchIndex..."
    # Disable credential helper to prevent interactive hang, push main
    git -c credential.helper= push origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to push batch $batchIndex"
        exit 1
    }
    
    $batchIndex++
}
Write-Host "Successfully completed batch push!"
