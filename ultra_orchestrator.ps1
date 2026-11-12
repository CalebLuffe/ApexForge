<#
.SYNOPSIS
    ApexForge MUD Client Suite - Ultra Orchestrator
.DESCRIPTION
    Standalone PowerShell bootstrapper for the ApexForge integrated engine.
    Provisions baseline JSON data files and launches the canonical Python runtime.
#>

[CmdletBinding()]
param(
    [string]$CharacterProfile,
    [string]$MudHost,
    [int]$Port,
    [string]$PathKey,
    [string]$TargetRoom,
    [switch]$NoConnect,
    [switch]$IpcBus,
    [switch]$Dashboard
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Initialize-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$Content
    )
    if (-not (Test-Path -LiteralPath $FilePath)) {
        Write-Host "[*] $(Split-Path -Leaf $FilePath) missing. Provisioning default..." -ForegroundColor Yellow
        $Content | Out-File -LiteralPath $FilePath -Encoding utf8
    }
}

# --- SYSTEM STAGE 1: AUTOMATED JSON DATA LAYER PROVISIONING ---
$DatabaseJson = @'
{
  "rooms": {
    "1": {"name": "The Grand Town Square", "x": 0, "y": 0, "zone": "town", "pre_move_cmd": "", "post_move_cmd": "", "min_room_wait": 0.0, "drops": [], "exits": {"north": {"target": "2", "weight": 1.0}, "east": {"target": "3", "weight": 5.0}}},
    "2": {"name": "The Dark Goblin Cave Entrance", "x": 0, "y": 1, "zone": "goblin_caves", "pre_move_cmd": "light torch", "post_move_cmd": "", "min_room_wait": 0.5, "drops": ["103"], "exits": {"south": {"target": "1", "weight": 1.0}, "east": {"target": "4", "weight": 2.0}}},
    "3": {"name": "The Rusty Anvil Blacksmith", "x": 1, "y": 0, "zone": "town", "pre_move_cmd": "", "post_move_cmd": "", "min_room_wait": 0.0, "drops": ["101"], "exits": {"west": {"target": "1", "weight": 1.0}, "north": {"target": "4", "weight": 10.0}}},
    "4": {"name": "Deep Goblin Caverns (Boss Room)", "x": 1, "y": 1, "zone": "goblin_caves", "pre_move_cmd": "", "post_move_cmd": "", "min_room_wait": 1.0, "drops": ["102", "104"], "exits": {"south": {"target": "2", "weight": 2.0}, "west": {"target": "3", "weight": 1.0}}}
  }
}
'@

$ItemsJson = @'
{
  "items": {
    "101": {"name": "Tattered Cloth Robe", "slot": "body", "ac": 2, "strength": 0, "intellect": 1, "min_level": 1, "weight": 2.0, "value": 5},
    "102": {"name": "Spiked Plate Mail", "slot": "body", "ac": 12, "strength": 3, "intellect": -1, "min_level": 10, "weight": 12.0, "value": 120},
    "103": {"name": "Serrated Steel Dagger", "slot": "weapon", "ac": 0, "strength": 1, "intellect": 0, "min_level": 1, "weight": 1.0, "value": 10},
    "104": {"name": "Warlord's Cleaver", "slot": "weapon", "ac": 2, "strength": 5, "intellect": 0, "min_level": 12, "weight": 8.0, "value": 200},
    "201": {"name": "Heavy Ore Chunk", "slot": "misc", "ac": 0, "strength": 0, "intellect": 0, "min_level": 1, "weight": 15.0, "value": 3},
    "202": {"name": "Cracked Shield", "slot": "offhand", "ac": 1, "strength": 0, "intellect": 0, "min_level": 1, "weight": 6.0, "value": 2}
  }
}
'@

$PathsJson = @'
{
  "paths": {
    "silver_mines_low": {"name": "Silver Mines (Level 1-10)", "loop_nodes": ["1", "2", "3", "1"], "zone_color_key": "silver_mines", "min_level": 1, "max_level": 10, "min_xp_hr": 20000, "min_lap_time": 0.0, "fallback_path": "town_square_safe"},
    "goblin_caverns_deep": {"name": "Deep Goblin Caverns (Level 10-20)", "loop_nodes": ["2", "4", "3", "2"], "zone_color_key": "goblin_caves", "min_level": 10, "max_level": 20, "min_xp_hr": 35000, "min_lap_time": 0.0, "fallback_path": "silver_mines_low"},
    "town_square_safe": {"name": "Town Safe Loop", "loop_nodes": ["1", "3", "1"], "zone_color_key": "town", "min_level": 1, "max_level": 99, "min_xp_hr": 0, "min_lap_time": 0.0, "fallback_path": null}
  }
}
'@

$ProfilesJson = @'
{
  "characters": {
    "Mofo": {
      "class": "Cleric",
      "race": "Dwarf",
      "host": "127.0.0.1",
      "port": 23,
      "discord_webhook": "",
      "leader_name": "Mofo",
      "admin_names": ["Mofo"],
      "tick_cast_command": "cast bless",
      "start_ipc_bus": false,
      "pre_move_command": "",
      "post_move_command": "",
      "survival": {"flee_hp_percentage": 30.0, "rest_hp_percentage": 70.0, "emergency_hangup_on_pvp": true, "auto_rest_command": "rest", "auto_stand_command": "stand"},
      "automation": {"restock_room_id": "1", "restock_commands": [], "corpse_loot_command": "get all corpse", "corpse_snipe_command": "get all from corpse", "interrupt_command": "bash", "spell_interrupt_command": "bash", "recovery_commands": ["look", "recall"], "burden_drop_threshold": 0.85, "max_carry_weight": 100.0, "drop_candidates": ["201", "202"]},
      "stat_weights": {"ac": 1.0, "strength": 0.8, "hp": 0.5, "intellect": 0.3}
    }
  }
}
'@

Initialize-JsonFile -FilePath (Join-Path $PSScriptRoot "database.json") -Content $DatabaseJson
Initialize-JsonFile -FilePath (Join-Path $PSScriptRoot "items.json") -Content $ItemsJson
Initialize-JsonFile -FilePath (Join-Path $PSScriptRoot "paths.json") -Content $PathsJson
Initialize-JsonFile -FilePath (Join-Path $PSScriptRoot "profiles.json") -Content $ProfilesJson

# --- SYSTEM STAGE 2: ENV VERIFICATION ---
Write-Host "[*] Verifying Python runtime..." -ForegroundColor Cyan
$pyExe = $null
try {
    $pyExe = (Get-Command python -ErrorAction Stop).Source
    $pyVersion = & $pyExe --version 2>&1
    Write-Host "[+] Found: $pyVersion" -ForegroundColor Green
} catch {
    $fallback = "D:\Users\notmy\AppData\Local\Programs\Python\Python312\python.exe"
    if (Test-Path -LiteralPath $fallback) { $pyExe = $fallback; Write-Host "[+] Using fallback: $pyExe" -ForegroundColor Green }
    else { throw "Python 3 must be installed and available on PATH." }
}

try { & $pyExe -c "import networkx" 2>$null | Out-Null } catch { Write-Host "[!] networkx not detected; mapper requires it (pip install networkx)." -ForegroundColor Yellow }
try { & $pyExe -c "import textual" 2>$null | Out-Null } catch { if ($Dashboard) { Write-Host "[!] textual not detected; dashboard requires it (pip install textual)." -ForegroundColor Yellow } }

# --- SYSTEM STAGE 3: LAUNCH CANONICAL ENGINE ---
$entry = if ($Dashboard) { "main.py" } else { "apexforge.py" }
$argsList = @()
if ($CharacterProfile) { $argsList += @("--profile", $CharacterProfile) }
if (-not $Dashboard) {
    if ($MudHost) { $argsList += @("--host", $MudHost) }
    if ($PSBoundParameters.ContainsKey('Port')) { $argsList += @("--port", "$Port") }
    if ($PathKey) { $argsList += @("--path", $PathKey) }
    if ($TargetRoom) { $argsList += @("--target", $TargetRoom) }
    if ($NoConnect) { $argsList += "--no-connect" }
    if ($IpcBus) { $argsList += "--ipc-bus" }
} else {
    if ($PSBoundParameters.ContainsKey('NoConnect')) { } # dashboard default is offline
    if (-not $NoConnect) { $argsList += "--connect" }
    if ($PathKey) { $argsList += @("--path", $PathKey) }
    if ($TargetRoom) { $argsList += @("--target", $TargetRoom) }
    if ($IpcBus) { $argsList += "--ipc-bus" }
}

Write-Host "[*] Launching $entry $($argsList -join ' ')" -ForegroundColor Cyan
& $pyExe (Join-Path $PSScriptRoot $entry) @argsList
exit $LASTEXITCODE
