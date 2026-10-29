# Collab Ops Manager

One Windows app for CUCM / IM&P and Cisco Unity Connection (CUC) maintenance that comes with its own SFTP server, so you don't need a separate SFTP application.

| Tab | What it does |
|---|---|
| **Cluster** | Connects to the publisher over SSH using the OS admin account, finds the nodes with `show network cluster`, shows each node's active and inactive version and installed COP files, and writes a pre/post-upgrade health report. |
| **SFTP Server** | The built-in SFTP server (paramiko). It is locked to one root folder, needs a password, and can be limited to cluster node IPs. It shows live transfers and has a button to add a Windows Firewall rule. |
| **Backup (DRS)** | Creates or updates the DRS network device that points at the built-in server, runs the backup, shows status and progress, and lists the backup files saved on this PC. Also shows history, device list and schedules, and can cancel a backup. |
| **Upgrade / COP** | Takes ISO/COP files placed in `<root>\upgrade` (with SHA-512/MD5 check), runs `utils system upgrade initiate` on the checked nodes one by one (publisher first) or all at once, shows upgrade status, and runs switch-version. |
| **Data Export** | Runs `file get` (TFTP files, traces, syslog, CDR, DRS and install logs, or a custom spec) from the checked nodes into `<root>\export\<node>\...`. Exports the configuration inventory to Excel over AXL SQL (phones, lines, DNs, users, patterns, trunks, device pools, CSS, or custom SQL). |
| **CLI Console** | A live SSH CLI session to any node, with quick-command buttons and masked password entry. |

## Unity Connection (CUC)
Enter the CUC publisher in the **Unity Connection cluster** box on the Cluster tab. **Connect & discover** then finds the nodes of both clusters, and each cluster uses its own OS admin account (leave the CUC one empty to use the CUCM account).
* **Backup:** pick *Unity Connection* on the Backup tab. It uses the `CUC` DRS feature and its own folder (`/backup_cuc`), so the two clusters' DRS rotation never deletes each other's backups. **Registered features** shows the names DRS accepts.
* **Upgrade / COP, file export, console:** CUC nodes appear in the node lists with type *Unity*.
* **Health check and dry run:** CUC nodes run `show cuc version`, `show cuc cluster status`, `show cuc dbserver disk` and `show cuc sysinfo`.
* **Inventory to Excel:** on the Data Export tab, pick *Unity Connection (CUPI REST)*. It exports users, call / directory / interview handlers, distribution lists, COS, templates, partitions, search spaces, routing rules, schedules, phone systems, port groups, ports and mailbox stores, plus any custom `/vmrest/...` path. It needs a CUC application administrator account and only reads (GET).

## Setup
1. Run `setup.bat` once. It installs the packages and adds Start Menu and Desktop shortcuts.
2. On the SFTP Server tab, pick the IP address CUCM uses to reach this PC and click **Allow port in Windows Firewall**.
3. Start the SFTP server. On the Cluster tab, enter the publisher and the OS admin account, then click **Connect & discover nodes**.

Notes
* CUCM sees the SFTP root as `/`. Use `/backup` for DRS and `/upgrade` for ISO/COP files.
* Port 22 is the default. If Windows OpenSSH Server is running on this PC, it already uses 22, so stop it or use another port. Older DRS releases cannot use a different port.
* DRS keeps the SFTP password in its backup device. If you change the password, run **Create / update backup device** again.
* Passwords are saved only when you tick *Remember passwords*. They are encrypted with Windows DPAPI for the current user.
* `tests/test_e2e.py`, `tests/test_dryrun.py` and `tests/test_cuc.py` run the flows against `tests/fake_cucm.py`, a simulated CLI and CUPI. They use a temporary config, never yours.
