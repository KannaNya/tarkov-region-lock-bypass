# Network model

The keeper operates at the IPv4 destination-route layer:

```text
Tarkov backend hostname -> current A records -> host /32 -> SoftEther VPN
ordinary destinations -> physical default route -> Japan ISP
Raid server IP/UDP -> no special route -> Japan ISP
```

The host set should come from current Launcher/EFT logs and DNS. In the observed flow, `gw-pvp`, `gw-pvp-season`, `lobby`, and `wsn-pvp-season-*` were involved around launch, profile selection, and second authorization. Shared IPs mean the route cannot distinguish individual URL paths.

## Verification checklist

```powershell
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0'
Find-NetRoute -RemoteIPAddress <current-backend-ip>
Test-NetConnection <current-backend-ip> -Port 443 -InformationLevel Detailed
curl.exe -4 https://ifconfig.co/country-iso
```

For Raid, capture the current game process connections and compare them with the route selected by `Find-NetRoute`; do not paste an old server list into a permanent configuration.

