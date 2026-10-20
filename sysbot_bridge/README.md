# 🌐 SysBot Bridge & Cloudflare Tunnel Gateway

A high-performance, asynchronous reverse proxy and cluster aggregator designed to run on your local PC alongside your 48 `SysBot.ACNHOrders` instances.

It exposes **a single HTTP port (8200)** that Cloudflare Tunnel can expose to the internet, allowing your **VPS-hosted Chobot** to access all 48 islands seamlessly without having to configure 48 individual Cloudflare tunnels or open ports.

---

## 🏗️ Architecture

```
┌─────────────────────────────────┐
│          VPS (Cloud)            │
│   Chobot (Discord / Web API)    │
└────────────────┬────────────────┘
                 │  HTTPS (Single Tunnel Domain)
                 ▼
┌─────────────────────────────────┐
│        Cloudflare Edge          │
│   e.g. sysbot.yourdomain.com    │
└────────────────┬────────────────┘
                 │  Encrypted Tunnel
                 ▼
┌─────────────────────────────────┐
│     Your Local PC (Windows)     │
│   cloudflared (Cloudflare CLI)  │
│                │                │
│                ▼ port 8200      │
│     [ SysBot Bridge App ]       │
│                │                │
│  0ms local bus ├─► :8201 (Hiraya)
│                ├─► :8202 (Tagumpay)
│                ├─► ...
│                └─► :8248 (Sinta)
└─────────────────────────────────┘
```

---

## 🚀 Quick Start (Local PC)

### 1. Configure Environment (Optional)
Copy `.env.example` to `.env`:
```cmd
copy .env.example .env
```
Set your `SYSBOT_API_KEY` (a shared secret between your PC and your VPS).

### 2. Start the Bridge
Double-click `run.bat` or run:
```cmd
python server.py
```
The bridge will listen on `http://localhost:8200`.

---

## 🛡️ Cloudflare Tunnel Setup (`cloudflared`)

On your local PC, configure your Cloudflare Tunnel (`config.yml`):

```yaml
tunnel: <YOUR-TUNNEL-UUID>
credentials-file: C:\Users\<Username>\.cloudflared\<YOUR-TUNNEL-UUID>.json

ingress:
  - hostname: sysbot.yourdomain.com
    service: http://localhost:8200
  - service: http_status:404
```

Then start the tunnel:
```cmd
cloudflared tunnel run
```

Now, all 48 island bots are accessible via **one single URL**: `https://sysbot.yourdomain.com`!

---

## ☁️ VPS Configuration (`chobot/.env`)

In your **Chobot `.env` file on your VPS**, simply set:

```env
# Point all island requests to your Cloudflare Tunnel bridge:
SYSBOT_URL_TEMPLATE="https://sysbot.yourdomain.com/islands/{island}"

# Secret API key (matches SYSBOT_API_KEY in your local bridge):
SYSBOT_API_KEY="chopaeng_secret_bridge_token_2026"
```

Chobot will automatically translate:
* `island = Sinta` ➡️ `https://sysbot.yourdomain.com/islands/sinta/api/status` ➡️ Local PC `:8248/api/status`
* `island = Hiraya` ➡️ `https://sysbot.yourdomain.com/islands/hiraya/api/status` ➡️ Local PC `:8201/api/status`
* `island = Dakila` ➡️ `https://sysbot.yourdomain.com/islands/dakila/api/control` ➡️ Local PC `:8215/api/control`

---

## ⚡ Unified Cluster Endpoints

The bridge also provides high-speed aggregated endpoints over your local network:

| Endpoint | Method | Description |
|---|---|---|
| `/health` | `GET` | Health check probe. |
| `/islands` | `GET` | Lists all 48 islands and their designated local ports. |
| `/cluster/status` | `GET` | **Parallel Multi-Poll**: Queries all 48 local SysBots over localhost and returns a single combined JSON in ~20ms! (Avoids sending 48 requests over Cloudflare tunnel). |
| `/cluster/telemetry` | `GET` | Aggregated hardware and connection state for all 48 islands. |
| `/islands/{island}/{path}` | `ANY` | Transparent proxy to the island's local SysBot HTTP API. |
| `/{island}/{path}` | `ANY` | Alias for transparent proxy. |

---

## 🧪 Testing

Test from your PC:
```cmd
curl http://localhost:8200/health
curl http://localhost:8200/islands
curl http://localhost:8200/islands/sinta/api/status
```

Test from VPS (over Cloudflare Tunnel):
```bash
curl -H "X-API-Key: chopaeng_secret_bridge_token_2026" https://sysbot.yourdomain.com/health
curl -H "X-API-Key: chopaeng_secret_bridge_token_2026" https://sysbot.yourdomain.com/cluster/status
curl -H "X-API-Key: chopaeng_secret_bridge_token_2026" https://sysbot.yourdomain.com/islands/sinta/api/status
```
