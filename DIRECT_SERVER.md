# Forge AI — Direct Server Mode

Forge can run as a private, persistent server instead of a public hosted website.

## What this mode provides

- Dockerized Forge Web server
- persistent Forge database in the Docker volume `forge-data`
- automatic container restart with `restart: unless-stopped`
- staged missions can continue after the browser/tab is closed while the server remains running
- port 8300 is bound on the server; no Render deployment is required
- optional private remote access through Tailscale

## 1. Start on a Debian/Ubuntu server

From a normal user with sudo access:

```bash
curl -fsSL https://raw.githubusercontent.com/priyanshagrahari54-blip/forge-ai/main/server/direct-server.sh | bash
```

The script installs Docker if needed, clones/updates Forge, creates the server workspace, and starts the Forge container.

For a server where you already cloned the repository:

```bash
cd forge-ai
docker compose up -d --build
docker compose ps
```

## 2. Local server access

On the server itself, the Forge service listens on port 8300.

```text
Forge Web -> 0.0.0.0:8300
Database  -> Docker volume forge-data
Workspace -> ./workspace
```

Do not publish port 8300 directly to the public Internet unless you have deliberately configured the required network and authentication controls.

## 3. Private remote access

Install Tailscale on the Forge server and on each device that should use Forge. Keep port 8300 private to the Tailscale network rather than opening it on the public firewall.

Conceptually:

```text
Laptop / Phone / PC
        |
    Tailscale
        |
 Forge Server :8300
        |
   Forge ControlPlane
        |
  persistent database
```

Tailscale is an optional networking layer; Forge itself does not depend on it.

## 4. Persistence and restart behaviour

The Docker volume `forge-data` stores `/data/forge`. The container uses `restart: unless-stopped`, so Docker will restart Forge after a process/container failure or host reboot when Docker itself is enabled at boot.

Mission state is stored by Forge's staged-build subsystem. The current autorun worker is intentionally fail-closed: it advances one stage only after the previous stage has verified acceptance, and it stops when a stage fails.

## 5. Configuration

Compose accepts these environment overrides:

- `FORGE_PORT` — host port, default `8300`
- `HOST` — default `0.0.0.0`
- `PORT` — container port, default `8300`
- `FORGE_AUTH_MODE` — default `production`
- `FORGE_SECURE_COOKIES` — default `0` for direct HTTP/private-network use; set to `1` only when Forge is actually served over HTTPS
- `FORGE_DB_PATH` — default `/data/forge/cockpit.db`
- `FORGE_PROJECT_ID` — default `forge`
- `FORGE_PROJECT_ROOT` — default `/workspace`
- `FORGE_ALLOWED_ORIGINS` — optional comma-separated browser origins

Example:

```bash
FORGE_PORT=8300 FORGE_ALLOWED_ORIGINS=http://100.x.x.x:8300 docker compose up -d --build
```

## Important distinction

This is a server application, so a browser still needs a network endpoint to render the Web UI. "No public URL" means Forge is not exposed as a public website; it can be reached only from the server itself, the LAN, or an authorized private network such as Tailscale.
