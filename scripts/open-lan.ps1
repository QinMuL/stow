# Stow LAN access: allow inbound TCP 8686 + Hyper-V firewall for WSL (with loopback)
netsh advfirewall firewall add rule name="Stow Web 8686" dir=in action=allow protocol=TCP localport=8686
Set-NetFirewallHyperVVMSetting -Name "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -DefaultInboundAction Allow
Set-NetFirewallHyperVVMSetting -Name "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -LoopbackEnabled $true
Read-Host "Done. Press Enter to close"
