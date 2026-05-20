// Drip — PM2 ecosystem
//
// Per the WSL PM2 gotcha: ABSOLUTE paths for cwd/script/interpreter.
// Relative or /mnt/c/... paths cause PM2 to fall back to $HOME on restart.
//
// Two processes:
//   1. drip-mock   → serve.py (x402 facilitator on :8090 + mock emitter on :8091)
//   2. drip-agent  → loop.py  (agent loop + dashboard on :8086)
//
// The Cloudflare tunnel for drip.baserep.xyz → localhost:8086 is added to
// /etc/cloudflared/config.yml and served by the existing systemd
// cloudflared.service (do not duplicate as a PM2 process).
//
// Start:    pm2 start ecosystem.config.js
// Save:     pm2 save --force
// Logs:     pm2 logs drip-agent
// Restart:  pm2 restart drip-agent
// Stop all: pm2 stop drip-mock drip-agent

const PROJECT_ROOT = "/mnt/c/Github/drip";
const POETRY_VENV  = "/mnt/c/Github/drip/.venv/bin/python";
const LOG_DIR      = "/home/vps/drip/logs";

module.exports = {
  apps: [
    {
      name: "drip-mock",
      cwd: PROJECT_ROOT,
      script: POETRY_VENV,
      args: "serve.py",

      // Log files (absolute paths — never use ./logs from /mnt/c/...)
      out_file:   `${LOG_DIR}/drip-mock.out.log`,
      error_file: `${LOG_DIR}/drip-mock.err.log`,
      merge_logs: true,
      time: true,

      // Restart behavior
      autorestart: true,
      max_restarts: 50,
      min_uptime: "10s",
      restart_delay: 4000,
      max_memory_restart: "300M",

      env: {
        NODE_ENV: "production",
      },
    },

    {
      name: "drip-agent",
      cwd: PROJECT_ROOT,
      script: POETRY_VENV,
      args: "loop.py",

      out_file:   `${LOG_DIR}/drip-agent.out.log`,
      error_file: `${LOG_DIR}/drip-agent.err.log`,
      merge_logs: true,
      time: true,

      autorestart: true,
      max_restarts: 50,
      min_uptime: "10s",
      // Wait 10s on restart so drip-mock has time to come up first
      restart_delay: 10000,
      max_memory_restart: "500M",

      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
