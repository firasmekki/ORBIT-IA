<#
.SYNOPSIS
  Démarrage automatique des serveurs de modèles d'ORBIT AI à l'ouverture de session Windows.

  Docker Desktop relance seul les conteneurs après un redémarrage, mais pas
  les deux llama-server.exe (modèle principal 8096, vision 8097) : sans eux
  le chat répond « Le modèle IA local n'est pas disponible ». Ce script ajoute
  (ou retire) un raccourci dans le dossier Démarrage de l'utilisateur, qui
  lance « orbit-models.ps1 start » - jamais deux instances (le script le
  vérifie), aucun droit administrateur, aucune tâche planifiée système.

.EXAMPLE
  .\orbit-autostart.ps1 install   # au prochain redémarrage, les modèles démarrent seuls
  .\orbit-autostart.ps1 status
  .\orbit-autostart.ps1 remove    # revient au démarrage manuel
#>
[CmdletBinding()]
param([Parameter(Position = 0)][ValidateSet('install', 'remove', 'status')][string]$Action = 'status')
$ErrorActionPreference = "Stop"

$startup = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup "ORBIT AI - modèles locaux.lnk"
$target = Join-Path $PSScriptRoot "orbit-models.ps1"

switch ($Action) {
    'install' {
        if (-not (Test-Path -LiteralPath $target)) { throw "orbit-models.ps1 introuvable : $target" }
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut)
        $link.TargetPath = (Get-Command powershell.exe).Source
        $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$target`" start"
        $link.WorkingDirectory = $PSScriptRoot
        $link.WindowStyle = 7  # minimisé
        $link.Description = "Démarre les serveurs de modèles locaux d'ORBIT AI (llama-server 8096 et 8097)"
        $link.Save()
        Write-Host "Démarrage automatique installé : $shortcut"
    }
    'remove' {
        if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut; Write-Host "Démarrage automatique retiré." }
        else { Write-Host "Aucun démarrage automatique n'était installé." }
    }
    'status' {
        Write-Host ("Démarrage automatique : {0}" -f $(if (Test-Path -LiteralPath $shortcut) { "installé ($shortcut)" } else { 'non installé' }))
    }
}
