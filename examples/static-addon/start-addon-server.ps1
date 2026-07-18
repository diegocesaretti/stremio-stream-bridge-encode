Set-Location $PSScriptRoot
Write-Host "Add-on disponible en http://IP_DE_ESTA_PC:7000/manifest.json"
Write-Host "Para cerrar, presiona Ctrl+C."
py -m http.server 7000 --bind 0.0.0.0
